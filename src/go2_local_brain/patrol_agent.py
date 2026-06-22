"""Headless, fully-autonomous LiDAR patrol agent for the Jetson.

Runs on the Jetson companion computer with no GUI and no LLM/Ollama. It connects
to the Go2 over WebRTC, streams the LiDAR voxel cloud into a ``LidarObstacleField``,
and drives a continuous obstacle-avoiding patrol using the pure planner in
``autonomy.patrol``. Intended to be supervised by systemd
(``deploy/jetson/go2-patrol.service``) and deployed over the host->dog->Jetson
SSH bridge (``scripts/deploy_patrol_to_jetson.sh``).

Safety
------
* Motion is gated behind ``--enable`` / ``GO2_PATROL_ENABLE=1``. Without it the
  agent still connects and logs every decision but never commands motion.
* It refuses to drive until LiDAR is fresh, unless ``GO2_PATROL_ALLOW_BLIND=1``.
* SIGINT/SIGTERM stop the robot and shut down cleanly; the driver deadman also
  halts the robot within ~1s if this loop stalls.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from dataclasses import replace
from typing import Any

from .autonomy.lidar_map import LidarObstacleField, LidarTransform, points_from_lidar_payload
from .autonomy.patrol import PatrolController, PatrolParams
from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient
from .viewer import _lidar_payload_from_message

log = logging.getLogger("go2.patrol")

_LIDAR_SWITCH_TOPIC = "rt/utlidar/switch"
_LIDAR_TOPIC = "rt/utlidar/voxel_map"
_LIDAR_ARRAY_TOPIC = "rt/utlidar/voxel_map_compressed"
_MAX_LIDAR_POINTS = 1400
_FIRST_LIDAR_TIMEOUT_S = 8.0


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def patrol_params_from_env() -> PatrolParams:
    """Build PatrolParams from GO2_PATROL_* env vars, falling back to defaults."""
    d = PatrolParams()
    return PatrolParams(
        forward_speed_mps=_env_float("GO2_PATROL_FORWARD_MPS", d.forward_speed_mps),
        backup_speed_mps=_env_float("GO2_PATROL_BACKUP_MPS", d.backup_speed_mps),
        turn_rate_rps=_env_float("GO2_PATROL_TURN_RPS", d.turn_rate_rps),
        stop_distance_m=_env_float("GO2_PATROL_STOP_M", d.stop_distance_m),
        slow_distance_m=_env_float("GO2_PATROL_SLOW_M", d.slow_distance_m),
        allow_blind=_env_bool("GO2_PATROL_ALLOW_BLIND", d.allow_blind),
    )


def _fmt(value: float | None) -> str:
    return "--" if value is None else f"{value:.2f}"


class PatrolAgent:
    """Connect, stream LiDAR, and run the patrol loop until told to stop."""

    def __init__(self, *, enabled: bool, max_seconds: float, params: PatrolParams) -> None:
        self._enabled = enabled
        self._max_seconds = max_seconds
        self._params = params
        self._client: Go2WebRTCClient | None = None
        self._field = LidarObstacleField()
        self._transform = LidarTransform.from_values(
            rotate_deg=os.getenv("GO2_LIDAR_ROTATE_DEG"),
            flip_x=os.getenv("GO2_LIDAR_FLIP_X"),
            flip_y=os.getenv("GO2_LIDAR_FLIP_Y"),
            swap_xy=os.getenv("GO2_LIDAR_SWAP_XY"),
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._lidar_raw = 0
        self._lidar_msgs = 0
        self._lidar_errors = 0

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()
        await self._connect()
        try:
            await self._patrol_until_stopped()
        finally:
            await self._shutdown()

    # -- connection + LiDAR ----------------------------------------------------

    def _install_signal_handlers(self) -> None:
        assert self._loop is not None
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # not supported on this platform; KeyboardInterrupt still works

    async def _connect(self) -> None:
        cfg = load_config()
        log.info("connecting to Go2 at %s via %s", cfg.go2_ip, cfg.go2_webrtc_method)
        self._client = Go2WebRTCClient(
            Go2Config(
                ip=cfg.go2_ip,
                aes_128_key=cfg.go2_aes_128_key,
                webrtc_method=cfg.go2_webrtc_method,
                serial_number=cfg.go2_serial_number,
                remote_username=cfg.go2_remote_username,
                remote_password=cfg.go2_remote_password,
                remote_region=cfg.go2_remote_region,
                remote_device_type=cfg.go2_remote_device_type,
                force_motion_mode=cfg.force_motion_mode,
            )
        )
        await self._client.connect()
        self._attach_lidar()
        log.info("connected; LiDAR subscribed")

    def _attach_lidar(self) -> None:
        conn = getattr(self._client, "_conn", None)
        datachannel = getattr(conn, "datachannel", None)
        if datachannel is None:
            log.warning("WebRTC datachannel unavailable; LiDAR disabled")
            return
        set_decoder = getattr(datachannel, "set_decoder", None)
        if callable(set_decoder):
            set_decoder(decoder_type="libvoxel")
        pubsub = getattr(datachannel, "pub_sub", None)
        if pubsub is None:
            log.warning("WebRTC pub_sub unavailable; LiDAR disabled")
            return
        try:
            pubsub.publish_without_callback(_LIDAR_SWITCH_TOPIC, "on")
            pubsub.subscribe(_LIDAR_TOPIC, self._on_lidar_message)
            pubsub.subscribe(_LIDAR_ARRAY_TOPIC, self._on_lidar_message)
        except Exception as exc:  # noqa: BLE001
            log.warning("LiDAR subscribe failed: %s", exc)

    def _detach_lidar(self) -> None:
        conn = getattr(self._client, "_conn", None)
        datachannel = getattr(conn, "datachannel", None)
        pubsub = getattr(datachannel, "pub_sub", None) if datachannel is not None else None
        if pubsub is None:
            return
        try:
            pubsub.publish_without_callback(_LIDAR_SWITCH_TOPIC, "off")
        except Exception as exc:  # noqa: BLE001
            log.debug("lidar switch off failed: %s", exc)

    def _on_lidar_message(self, message: Any) -> None:
        # Called from the SDK's receive thread; hop back to the event loop.
        self._lidar_raw += 1
        payload = _lidar_payload_from_message(message, max_points=_MAX_LIDAR_POINTS)
        if payload is None:
            self._lidar_errors += 1
            return
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._ingest_lidar, payload)

    def _ingest_lidar(self, payload: dict[str, Any]) -> None:
        points = self._transform.apply(points_from_lidar_payload(payload))
        self._field.update(points)
        self._lidar_msgs += 1

    async def _await_first_lidar(self, timeout_s: float = _FIRST_LIDAR_TIMEOUT_S) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._field.current_summary().fresh:
                return True
            await asyncio.sleep(0.1)
        return False

    # -- patrol loop -----------------------------------------------------------

    async def _patrol_until_stopped(self) -> None:
        assert self._client is not None
        if not self._enabled:
            log.warning("DRY RUN: motion disabled. Use --enable or GO2_PATROL_ENABLE=1 to patrol.")
        else:
            log.info("standing up for patrol")
            await self._client.stand_up()
            await asyncio.sleep(1.0)

        if not await self._await_first_lidar():
            if not self._params.allow_blind:
                log.error(
                    "no fresh LiDAR after %.0fs; refusing to drive "
                    "(set GO2_PATROL_ALLOW_BLIND=1 to override). Will keep holding.",
                    _FIRST_LIDAR_TIMEOUT_S,
                )
            else:
                log.warning("no LiDAR yet; proceeding in blind mode")
        else:
            log.info("LiDAR is live; beginning patrol")

        controller = PatrolController(self._params)
        deadline = time.monotonic() + self._max_seconds if self._max_seconds > 0 else None
        last_forward_log = 0.0
        steps = 0

        while not self._stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                log.info("max-seconds (%.0f) reached; ending patrol", self._max_seconds)
                break
            summary = self._field.current_summary()
            decision = controller.step(summary)
            steps += 1

            now = time.monotonic()
            if decision.action != "forward" or now - last_forward_log > 2.0:
                log.info(
                    "step %d: %s [lidar f=%s l=%s r=%s pts=%d fresh=%s msgs=%d]",
                    steps, decision.note, _fmt(summary.front_m), _fmt(summary.left_m),
                    _fmt(summary.right_m), summary.point_count, summary.fresh, self._lidar_msgs,
                )
                last_forward_log = now

            if self._enabled and decision.moves:
                await self._client.move(decision.vx, decision.vy, decision.vyaw, decision.duration_s)
            else:
                await self._sleep_or_stop(decision.duration_s)

        log.info("patrol loop exited after %d steps", steps)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, seconds))
        except asyncio.TimeoutError:
            pass

    async def _shutdown(self) -> None:
        if self._client is None:
            return
        log.info("shutting down: stopping robot")
        try:
            await self._client.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("stop failed: %s", exc)
        self._detach_lidar()
        try:
            await self._client.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("close failed: %s", exc)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Headless autonomous LiDAR patrol for the Go2 (Jetson).")
    p.add_argument("--enable", action="store_true", help="actually move the robot (default: dry run)")
    p.add_argument("--allow-blind", action="store_true", help="patrol even without LiDAR (risky)")
    p.add_argument("--max-seconds", type=float, default=0.0, help="stop after N seconds (0 = forever)")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    enabled = args.enable or _env_bool("GO2_PATROL_ENABLE", False)
    max_seconds = args.max_seconds or _env_float("GO2_PATROL_MAX_SECONDS", 0.0)
    params = patrol_params_from_env()
    if args.allow_blind:
        params = replace(params, allow_blind=True)
    log.info(
        "patrol config: enabled=%s allow_blind=%s max_seconds=%s forward=%.2fm/s stop=%.2fm slow=%.2fm",
        enabled, params.allow_blind, max_seconds or "forever",
        params.forward_speed_mps, params.stop_distance_m, params.slow_distance_m,
    )
    await PatrolAgent(enabled=enabled, max_seconds=max_seconds, params=params).run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
