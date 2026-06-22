"""Headless full autonomy agent for the Jetson: video-out + roam + follow.

Everything runs on the Jetson, no GUI and no prompts/LLM. It:

* connects to the Go2 over WebRTC and serves a plain MJPEG video stream so a
  laptop can just watch (``http://<jetson-ip>:8788/``),
* streams the LiDAR voxel cloud into a ``LidarObstacleField``,
* runs perception (YOLO person detection + OpenCV face boxes) on the video,
* and drives a follow-then-roam behaviour: follow the nearest person while one
  is visible (LiDAR-gated so it never walks into a wall), briefly scan after
  losing them, then fall back to continuous LiDAR roam.

Safety mirrors ``patrol_agent``: motion is gated behind ``--enable`` /
``GO2_AUTONOMY_ENABLE=1`` (dry run otherwise), SIGINT/SIGTERM stop the robot,
and the driver deadman halts motion if the loop stalls. If the person detector
can't load (e.g. no GPU/torch), perception simply yields no people and the robot
keeps roaming on LiDAR + streaming video.
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

from aiohttp import web

from .autonomy.behavior import MODE_FOLLOW, MODE_ROAM, MODE_SCAN, gate_follow_with_lidar, select_mode
from .autonomy.follow import HumanFollowController
from .autonomy.lidar_map import LidarObstacleField, LidarTransform, points_from_lidar_payload
from .autonomy.patrol import PatrolController
from .autonomy.perception import Observation, YoloPerceptionProvider, best_human_detection
from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient
from .patrol_agent import _env_bool, _env_float, patrol_params_from_env
from .viewer import _jpeg_from_frame, _lidar_payload_from_message

log = logging.getLogger("go2.autonomy")

_LIDAR_SWITCH_TOPIC = "rt/utlidar/switch"
_LIDAR_TOPIC = "rt/utlidar/voxel_map"
_LIDAR_ARRAY_TOPIC = "rt/utlidar/voxel_map_compressed"
_MAX_LIDAR_POINTS = 1400
_SCAN_YAW_RPS = 0.45
_SCAN_STEP_S = 0.40


def _fmt(value: float | None) -> str:
    return "--" if value is None else f"{value:.2f}"


class AutonomyAgent:
    def __init__(
        self,
        *,
        enabled: bool,
        host: str,
        port: int,
        max_seconds: float,
        patrol_params: Any,
        follow_grace_s: float,
        detector_model: str,
        detector_threshold: float,
        detector_device: str | None,
        perception_interval_s: float,
    ) -> None:
        self._enabled = enabled
        self._host = host
        self._port = port
        self._max_seconds = max_seconds
        self._patrol_params = patrol_params
        self._patrol = PatrolController(patrol_params)
        self._follow_grace_s = follow_grace_s
        self._detector_model = detector_model
        self._detector_threshold = detector_threshold
        self._detector_device = detector_device
        self._perception_interval_s = perception_interval_s

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
        self._changed = asyncio.Condition()
        self._latest_jpeg: bytes | None = None
        self._latest_video_ts = 0.0
        self._video_frames = 0
        self._observation = Observation(timestamp=0.0, frame_available=False)
        self._follow: HumanFollowController | None = None
        self._perception: YoloPerceptionProvider | None = None
        self._perception_task: asyncio.Task[None] | None = None
        self._mode = "roam"
        self._last_note = "starting"
        self._lidar_msgs = 0

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()
        runner = await self._start_web()
        try:
            await self._connect()
            self._build_perception()
            self._perception_task = asyncio.create_task(self._perception_loop(), name="go2-perception")
            await self._behavior_loop()
        finally:
            await self._shutdown()
            await runner.cleanup()

    # -- web (video to the laptop) --------------------------------------------

    async def _start_web(self) -> web.AppRunner:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/video.mjpg", self._video_stream)
        app.router.add_get("/status.json", self._status_json)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, self._host, self._port).start()
        log.info("video stream live at http://%s:%s/", self._host, self._port)
        return runner

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _status_json(self, _request: web.Request) -> web.Response:
        s = self._field.current_summary()
        return web.json_response(
            {
                "mode": self._mode,
                "note": self._last_note,
                "enabled": self._enabled,
                "video_frames": self._video_frames,
                "lidar": {"front_m": s.front_m, "left_m": s.left_m, "right_m": s.right_m,
                          "fresh": s.fresh, "points": s.point_count, "msgs": self._lidar_msgs},
                "detections": self._observation.summary(),
            }
        )

    async def _video_stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"}
        )
        await response.prepare(request)
        last_sent = 0.0
        while not self._stop.is_set():
            async with self._changed:
                await self._changed.wait_for(
                    lambda: self._latest_jpeg is not None and self._latest_video_ts != last_sent
                )
                jpeg = self._latest_jpeg
                last_sent = self._latest_video_ts
            if jpeg is not None:
                await response.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
        return response

    # -- connection: video + lidar --------------------------------------------

    def _install_signal_handlers(self) -> None:
        assert self._loop is not None
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass

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
        self._attach_video()
        self._attach_lidar()
        log.info("connected; video + LiDAR attached")

    def _attach_video(self) -> None:
        conn = getattr(self._client, "_conn", None)
        video = getattr(conn, "video", None)
        if video is None:
            log.warning("WebRTC video interface not found; stream will be blank")
            return
        video.switchVideoChannel(True)
        video.add_track_callback(self._recv_video_track)

    async def _recv_video_track(self, track: Any) -> None:
        while not self._stop.is_set():
            frame = await track.recv()
            jpeg = _jpeg_from_frame(frame)
            async with self._changed:
                self._latest_jpeg = jpeg
                self._latest_video_ts = time.time()
                self._video_frames += 1
                self._changed.notify_all()

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
        payload = _lidar_payload_from_message(message, max_points=_MAX_LIDAR_POINTS)
        if payload is None:
            return
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._ingest_lidar, payload)

    def _ingest_lidar(self, payload: dict[str, Any]) -> None:
        self._field.update(self._transform.apply(points_from_lidar_payload(payload)))
        self._lidar_msgs += 1

    # -- perception ------------------------------------------------------------

    def _build_perception(self) -> None:
        self._perception = YoloPerceptionProvider(
            frame_supplier=lambda: self._latest_jpeg,
            model_name=self._detector_model,
            threshold=self._detector_threshold,
            device=self._detector_device,
            detect_faces=True,
        )

    async def _perception_loop(self) -> None:
        assert self._perception is not None
        while not self._stop.is_set():
            try:
                self._observation = await self._perception.observe()
            except Exception as exc:  # noqa: BLE001
                log.debug("perception error: %s", exc)
                self._observation = Observation(
                    timestamp=time.time(), frame_available=self._latest_jpeg is not None, note=str(exc)
                )
            await self._sleep_or_stop(self._perception_interval_s)

    # -- behaviour -------------------------------------------------------------

    async def _behavior_loop(self) -> None:
        assert self._client is not None
        self._follow = HumanFollowController(self._client)
        if not self._enabled:
            log.warning("DRY RUN: motion disabled. Use --enable or GO2_AUTONOMY_ENABLE=1 to move.")
        else:
            log.info("standing up")
            await self._client.stand_up()
            await asyncio.sleep(1.0)

        deadline = time.monotonic() + self._max_seconds if self._max_seconds > 0 else None
        last_person_ts = 0.0
        last_log = 0.0
        steps = 0

        while not self._stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                log.info("max-seconds reached; ending")
                break
            obs = self._observation
            summary = self._field.current_summary()
            now = time.monotonic()
            person = best_human_detection(obs)
            if person is not None:
                last_person_ts = now
            mode = select_mode(person is not None, now - last_person_ts, follow_grace_s=self._follow_grace_s)
            self._mode = mode

            if mode == MODE_FOLLOW and self._follow is not None:
                cmd = self._follow.plan(obs)
                gated = gate_follow_with_lidar(
                    cmd.vx, cmd.vyaw, cmd.duration_s, summary,
                    stop_distance_m=self._patrol_params.stop_distance_m,
                    turn_rate_rps=self._patrol_params.turn_rate_rps,
                )
                vx, vy, vyaw, dur = gated.vx, gated.vy, gated.vyaw, gated.duration_s
                note = f"FOLLOW {cmd.reason} | {gated.note}"
            elif mode == MODE_SCAN:
                vx, vy, vyaw, dur = 0.0, 0.0, _SCAN_YAW_RPS, _SCAN_STEP_S
                note = "SCAN for person"
            else:  # MODE_ROAM
                d = self._patrol.step(summary)
                vx, vy, vyaw, dur = d.vx, d.vy, d.vyaw, d.duration_s
                note = f"ROAM {d.note}"

            self._last_note = note
            steps += 1
            if mode != MODE_ROAM or person is not None or now - last_log > 2.0:
                log.info(
                    "step %d [%s] %s | lidar f=%s l=%s r=%s fresh=%s | %s",
                    steps, mode.upper(), note, _fmt(summary.front_m), _fmt(summary.left_m),
                    _fmt(summary.right_m), summary.fresh, obs.summary(),
                )
                last_log = now

            if self._enabled and (vx, vy, vyaw) != (0.0, 0.0, 0.0):
                await self._client.move(vx, vy, vyaw, dur)
            else:
                await self._sleep_or_stop(dur)

        log.info("behaviour loop exited after %d steps", steps)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, seconds))
        except asyncio.TimeoutError:
            pass

    async def _shutdown(self) -> None:
        self._stop.set()
        if self._perception_task is not None:
            self._perception_task.cancel()
            try:
                await self._perception_task
            except asyncio.CancelledError:
                pass
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


_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Go2 Autonomy</title>
<style>
  html,body{margin:0;height:100%;background:#000;color:#fff;font:13px system-ui,sans-serif;}
  main{position:relative;height:100%;display:grid;place-items:center;}
  img{max-width:100%;max-height:100%;object-fit:contain;}
  #hud{position:absolute;left:12px;top:12px;padding:8px 12px;border-radius:8px;
       background:rgba(0,0,0,.6);backdrop-filter:blur(6px);white-space:pre;}
</style></head>
<body><main>
  <img id="v" src="/video.mjpg" alt="Go2 live video">
  <div id="hud">connecting…</div>
</main>
<script>
  async function tick(){
    try{
      const s = await (await fetch('/status.json')).json();
      document.getElementById('hud').textContent =
        `mode: ${s.mode}` + (s.enabled ? '' : ' (DRY RUN)') + `\\n` +
        `${s.note}\\n` +
        `lidar f=${s.lidar.front_m??'--'} l=${s.lidar.left_m??'--'} r=${s.lidar.right_m??'--'} ` +
        `fresh=${s.lidar.fresh}\\n${s.detections}`;
    }catch(e){}
  }
  setInterval(tick, 700); tick();
</script>
</body></html>"""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Headless Go2 autonomy: video + roam + follow (Jetson).")
    p.add_argument("--enable", action="store_true", help="actually move the robot (default: dry run)")
    p.add_argument("--allow-blind", action="store_true", help="roam even without LiDAR (risky)")
    p.add_argument("--host", default=os.getenv("GO2_AUTONOMY_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.getenv("GO2_AUTONOMY_PORT", "8788")))
    p.add_argument("--max-seconds", type=float, default=0.0, help="stop after N seconds (0 = forever)")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    enabled = args.enable or _env_bool("GO2_AUTONOMY_ENABLE", False)
    max_seconds = args.max_seconds or _env_float("GO2_AUTONOMY_MAX_SECONDS", 0.0)
    params = patrol_params_from_env()
    if args.allow_blind:
        params = replace(params, allow_blind=True)
    device = os.getenv("GO2_DETECTOR_DEVICE", "").strip() or None
    agent = AutonomyAgent(
        enabled=enabled,
        host=args.host,
        port=args.port,
        max_seconds=max_seconds,
        patrol_params=params,
        follow_grace_s=_env_float("GO2_FOLLOW_GRACE_S", 2.5),
        detector_model=os.getenv("GO2_DETECTOR_MODEL", "yolov8n.pt"),
        detector_threshold=_env_float("GO2_DETECTOR_THRESHOLD", 0.55),
        detector_device=device,
        perception_interval_s=_env_float("GO2_PERCEPTION_INTERVAL_S", 0.25),
    )
    log.info(
        "autonomy config: enabled=%s host=%s port=%s detector=%s device=%s follow_grace=%.1fs",
        enabled, args.host, args.port, agent._detector_model, device or "auto", agent._follow_grace_s,
    )
    await agent.run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
