"""Thin async wrapper around unitree_webrtc_connect for the Go2 Air.

Design notes
------------
* All WebRTC work stays on the asyncio loop. We never block it; every
  internal call is awaited or non-blocking, and tight refresh loops yield
  via ``asyncio.sleep``.
* The upstream package (``unitree_webrtc_connect`` 2.x) doesn't expose a
  ``SportClient`` - high-level locomotion is sent as JSON-RPC-ish requests
  over a WebRTC data channel using ``RTC_TOPIC["SPORT_MOD"]`` and the
  ``SPORT_CMD`` api-id table. We hide that behind ``stand_up`` /
  ``sit_down`` / ``stop`` / ``move`` so the rest of the app doesn't care.
* Some attribute names (``aes_128_key`` vs ``aesKey``, the data-channel
  pub/sub attribute, etc.) have varied across upstream releases. Where the
  cost is small we probe for them at runtime so a minor version bump
  doesn't break us.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..safety.limits import (
    DEADMAN_TIMEOUT_S,
    DEFAULT_MOVE_DURATION_S,
    MAX_VX,
    MAX_VY,
    MAX_VYAW,
    clamp,
)

log = logging.getLogger(__name__)

# Rate at which an in-flight move() refreshes the velocity command.
_MOVE_REFRESH_HZ = 20.0
_MOVE_REFRESH_PERIOD = 1.0 / _MOVE_REFRESH_HZ

# Rate at which the deadman loop checks staleness.
_DEADMAN_TICK_S = 0.1

# How long to wait for the data channel to become writable after connect().
# Matches the upstream waiter's own default; slower LAN setups legitimately
# need this much (per upstream comment in webrtc_datachannel.py).
_DC_OPEN_TIMEOUT_S = 15.0

# After StandUp we wait briefly before switching the controller into
# BalanceStand. The firmware's stand-up animation takes ~2 s; if we issue
# BalanceStand too early the request can be ignored.
_STAND_TO_BALANCE_PAUSE_S = 2.5

# Motion-mode switcher (`rt/api/motion_switcher/request`) API ids the
# upstream `examples/go2/data_channel/sportmode/sportmode.py` uses. These
# aren't exposed as a constants dict by the package, so we hardcode them.
_MOTION_SWITCHER_CHECK_API = 1001
_MOTION_SWITCHER_SET_API = 1002

# How long to wait after switching motion mode for the controller to
# stabilize. Mirrors the upstream example's sleep.
_MOTION_MODE_SETTLE_S = 5.0


@dataclass
class Go2Config:
    """Connection parameters for the robot."""

    ip: str
    aes_128_key: Optional[str] = None
    # If set (e.g. "normal" / "mcf"), connect() will switch the robot into
    # that motion mode before starting the deadman and accepting commands.
    # Leave None to skip - useful when a custom firmware/package wants to
    # stay in its own pre-selected mode.
    force_motion_mode: Optional[str] = None


class Go2WebRTCClient:
    """Async-friendly wrapper around ``UnitreeWebRTCConnection``.

    Public surface: ``connect()``, ``close()``, ``stand_up()``,
    ``sit_down()``, ``stop()``, ``move(vx, vy, vyaw, duration_s)``.
    """

    def __init__(self, cfg: Go2Config) -> None:
        self._cfg = cfg
        self._conn: Any = None  # unitree_webrtc_connect.UnitreeWebRTCConnection
        self._pubsub: Any = None
        self._sport_topic: Optional[str] = None
        self._sport_state_topic: Optional[str] = None
        self._motion_switcher_topic: Optional[str] = None
        self._sport_cmd: dict[str, int] = {}

        self._last_cmd_ts: float = 0.0
        self._deadman_task: Optional[asyncio.Task[None]] = None
        self._move_task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()

        # Passive sport-state telemetry. We log transitions but (deliberately,
        # for now) do not gate motion on it - schema/enum semantics aren't
        # verified on this firmware. Phase 2: refuse move() on fault.
        self._sport_state: dict[str, Any] = {}
        self._sport_state_ts: float = 0.0
        self._sport_state_summary: Optional[tuple[Any, Any]] = None

    # ------------------------------------------------------------------ connect

    async def connect(self) -> None:
        """Open WebRTC, wait for the data channel, and cache the sport topic."""
        from unitree_webrtc_connect import (  # type: ignore
            RTC_TOPIC,
            SPORT_CMD,
            UnitreeWebRTCConnection,
            WebRTCConnectionMethod,
        )

        # Build constructor kwargs defensively - different point releases
        # of unitree_webrtc_connect have spelled the key argument both ways.
        kwargs: dict[str, Any] = {
            "connectionMethod": WebRTCConnectionMethod.LocalSTA,
            "ip": self._cfg.ip,
        }
        if self._cfg.aes_128_key:
            kwargs["aes_128_key"] = self._cfg.aes_128_key

        try:
            self._conn = UnitreeWebRTCConnection(**kwargs)
        except TypeError:
            kwargs.pop("aes_128_key", None)
            if self._cfg.aes_128_key:
                kwargs["aesKey"] = self._cfg.aes_128_key
            self._conn = UnitreeWebRTCConnection(**kwargs)

        await self._conn.connect()

        # Locate the pub/sub object. Upstream exposes it as
        # ``conn.datachannel.pub_sub`` today; fall back to a couple of
        # near-spellings if a future release renames it.
        self._pubsub = self._find_pubsub(self._conn)
        if self._pubsub is None:
            raise RuntimeError("WebRTC data channel pub/sub interface not found")

        # Wait for the data channel to actually be open before publishing.
        await self._await_datachannel_ready()

        self._sport_topic = RTC_TOPIC["SPORT_MOD"]
        # Upstream's own sportmodestate example subscribes to LF, not the
        # plain SPORT_MOD_STATE. The LF stream is the one with full posture
        # + IMU fields we care about.
        self._sport_state_topic = RTC_TOPIC.get("LF_SPORT_MOD_STATE") or RTC_TOPIC.get("SPORT_MOD_STATE")
        self._motion_switcher_topic = RTC_TOPIC.get("MOTION_SWITCHER")
        self._sport_cmd = dict(SPORT_CMD)

        # Optional motion-mode pre-flight. Off by default - Cooper's custom
        # 1.1.7 package may already pick the right mode and forcing a switch
        # would fight it. Set FORCE_MOTION_MODE in .env to opt in.
        if self._cfg.force_motion_mode:
            await self._force_motion_mode(self._cfg.force_motion_mode)

        # Passive subscription: log posture/error transitions, never block on them.
        if self._sport_state_topic:
            try:
                self._pubsub.subscribe(self._sport_state_topic, self._on_sport_state)
            except Exception as exc:  # noqa: BLE001
                log.warning("sport state subscribe failed: %s", exc)

        log.info("Go2 WebRTC connected at %s", self._cfg.ip)

        # Start the deadman as soon as we can send.
        self._last_cmd_ts = time.monotonic()
        self._deadman_task = asyncio.create_task(
            self._deadman_loop(), name="go2-deadman"
        )

    # -------------------------------------------------------------------- close

    async def close(self) -> None:
        """Cancel background tasks and tear down the WebRTC link."""
        await self._cancel_move_task()

        if self._deadman_task is not None:
            self._deadman_task.cancel()
            try:
                await self._deadman_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._deadman_task = None

        # Best-effort: stop the robot before tearing down.
        try:
            if self._pubsub is not None:
                await self._send_stop_move()
        except Exception as exc:  # noqa: BLE001
            log.warning("stop during close failed: %s", exc)

        if self._conn is not None:
            try:
                await self._conn.disconnect()
            except Exception as exc:  # noqa: BLE001
                log.warning("connection disconnect failed: %s", exc)
        self._conn = None
        self._pubsub = None
        log.info("Go2 WebRTC closed")

    # ----------------------------------------------------------- high-level cmds

    async def stand_up(self) -> None:
        """Stand up, then enter BalanceStand so subsequent Move calls are accepted.

        The firmware exposes a locked StandUp posture (1004) and a separate
        active-balance mode (BalanceStand, 1002). Move requests are honored
        in BalanceStand. We chain the two with a short pause for the standup
        animation; if a downstream package doesn't expose BalanceStand we
        skip it.
        """
        await self._sport_request("StandUp")
        if "BalanceStand" in self._sport_cmd:
            await asyncio.sleep(_STAND_TO_BALANCE_PAUSE_S)
            await self._sport_request("BalanceStand")
        self._touch()

    async def sit_down(self) -> None:
        # Prefer the controlled "StandDown" lower over the abrupt "Sit"
        # when both are present.
        await self._sport_request_first(["StandDown", "Sit"])
        self._touch()

    async def stop(self) -> None:
        """Zero-velocity + cancel any pending move loop.

        Resilient: this is the universal panic button (the brain's error
        handler calls it after any tool failure), so we must not raise.
        Underlying publish failures are logged and swallowed.
        """
        await self._cancel_move_task()
        try:
            await self._send_stop_move()
        except Exception as exc:  # noqa: BLE001
            log.warning("stop publish failed: %s", exc)
        self._touch()

    async def move(
        self,
        vx: float,
        vy: float = 0.0,
        vyaw: float = 0.0,
        duration_s: float = DEFAULT_MOVE_DURATION_S,
    ) -> None:
        """Drive at the given (clamped) velocity for ``duration_s`` seconds.

        Internally this spawns a short-lived task that re-publishes the
        velocity at ~20 Hz, then stops. Sequential calls cancel the previous
        in-flight movement instead of stacking up.
        """
        cvx = clamp(vx, -MAX_VX, MAX_VX)
        cvy = clamp(vy, -MAX_VY, MAX_VY)
        cvyaw = clamp(vyaw, -MAX_VYAW, MAX_VYAW)
        dur = max(0.0, float(duration_s))
        if (cvx, cvy, cvyaw) != (vx, vy, vyaw):
            log.info(
                "move clamped: requested vx=%.3f vy=%.3f vyaw=%.3f -> vx=%.3f vy=%.3f vyaw=%.3f",
                vx, vy, vyaw, cvx, cvy, cvyaw,
            )

        await self._cancel_move_task()
        self._move_task = asyncio.create_task(
            self._move_loop(cvx, cvy, cvyaw, dur), name="go2-move"
        )
        try:
            await self._move_task
        except asyncio.CancelledError:
            # Caller cancelled or a newer move() superseded us; that's fine.
            pass

    # -------------------------------------------------------------------- inner

    async def _move_loop(
        self, vx: float, vy: float, vyaw: float, duration_s: float
    ) -> None:
        """Refresh the velocity at _MOVE_REFRESH_HZ, then publish a final stop."""
        deadline = time.monotonic() + duration_s
        try:
            while time.monotonic() < deadline:
                self._publish_move(vx, vy, vyaw)
                self._touch()
                remaining = deadline - time.monotonic()
                await asyncio.sleep(min(_MOVE_REFRESH_PERIOD, max(0.0, remaining)))
        finally:
            # Always settle to zero, even if cancelled mid-flight.
            try:
                await self._send_stop_move()
            except Exception as exc:  # noqa: BLE001
                log.warning("trailing stop after move failed: %s", exc)
            self._touch()

    async def _cancel_move_task(self) -> None:
        task = self._move_task
        if task is None or task.done():
            self._move_task = None
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._move_task = None

    async def _deadman_loop(self) -> None:
        """If commands go stale, force a zero velocity. Never blocks the loop."""
        try:
            while True:
                await asyncio.sleep(_DEADMAN_TICK_S)
                age = time.monotonic() - self._last_cmd_ts
                if age > DEADMAN_TIMEOUT_S:
                    try:
                        self._publish_move(0.0, 0.0, 0.0)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("deadman zero-vel send failed: %s", exc)
                    # Reset the clock until a real command arrives, to
                    # avoid spamming.
                    self._last_cmd_ts = time.monotonic()
        except asyncio.CancelledError:
            return

    # --------------------------------------------------------- sport publishing

    def _publish_move(self, vx: float, vy: float, vyaw: float) -> None:
        """Fire-and-forget Move at the given velocity.

        We deliberately use ``publish_without_callback`` here because the
        20 Hz refresh loop cannot afford to await a per-message response.
        Upstream's publish_without_callback silently no-ops when the channel
        is closed, so we check readyState ourselves and surface the failure.
        """
        if self._pubsub is None or self._sport_topic is None:
            raise RuntimeError("not connected: call connect() first")
        channel = getattr(self._pubsub, "channel", None)
        if channel is not None and getattr(channel, "readyState", None) != "open":
            raise RuntimeError("data channel is not open")
        api_id = self._sport_cmd.get("Move")
        if api_id is None:
            raise RuntimeError("SPORT_CMD['Move'] missing from package")
        payload = _build_sport_request(api_id, {"x": float(vx), "y": float(vy), "z": float(vyaw)})
        # publish_without_callback is sync (channel.send underneath).
        self._pubsub.publish_without_callback(
            self._sport_topic, payload, _request_msg_type()
        )

    async def _send_stop_move(self) -> None:
        """Acknowledged StopMove. Used for explicit stop() and during close()."""
        api_id = self._sport_cmd.get("StopMove")
        if api_id is None:
            # Fall back to a zero-velocity Move so we still calm the robot.
            self._publish_move(0.0, 0.0, 0.0)
            return
        async with self._lock:
            await self._pubsub.publish_request_new(
                self._sport_topic, {"api_id": api_id}
            )

    async def _sport_request(self, cmd_name: str) -> None:
        api_id = self._sport_cmd.get(cmd_name)
        if api_id is None:
            raise RuntimeError(f"SPORT_CMD[{cmd_name!r}] missing from package")
        async with self._lock:
            await self._pubsub.publish_request_new(
                self._sport_topic, {"api_id": api_id}
            )

    async def _sport_request_first(self, candidates: list[str]) -> None:
        for name in candidates:
            if name in self._sport_cmd:
                await self._sport_request(name)
                return
        raise RuntimeError(f"none of {candidates} are in SPORT_CMD")

    # -------------------------------------------------------- internal helpers

    @staticmethod
    def _find_pubsub(conn: Any) -> Any:
        """Locate the data-channel pub/sub object across known layouts."""
        dc = getattr(conn, "datachannel", None) or getattr(conn, "data_channel", None)
        if dc is None:
            return None
        return (
            getattr(dc, "pub_sub", None)
            or getattr(dc, "pubsub", None)
            or getattr(dc, "publisher", None)
        )

    async def _await_datachannel_ready(self) -> None:
        """Block until the data channel is open enough to publish."""
        dc = getattr(self._conn, "datachannel", None) or getattr(
            self._conn, "data_channel", None
        )
        if dc is None:
            return
        # Prefer the package's own waiter if it exists; otherwise poll a
        # bool flag.
        waiter = getattr(dc, "wait_datachannel_open", None)
        if callable(waiter):
            try:
                # Upstream signature is wait_datachannel_open(timeout=15).
                # Try with the kwarg first; if a future version renames it,
                # fall back to no-arg + our own wait_for.
                try:
                    result = waiter(timeout=_DC_OPEN_TIMEOUT_S)
                except TypeError:
                    result = waiter()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=_DC_OPEN_TIMEOUT_S)
                return
            except asyncio.TimeoutError:
                raise RuntimeError("data channel did not open in time")
            except Exception as exc:  # noqa: BLE001
                log.debug("wait_datachannel_open() failed (%s); polling flag", exc)
        deadline = time.monotonic() + _DC_OPEN_TIMEOUT_S
        while time.monotonic() < deadline:
            if bool(getattr(dc, "data_channel_opened", False)):
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("data channel did not open in time")

    def _touch(self) -> None:
        """Mark the latest time we issued any command. Feeds the deadman."""
        self._last_cmd_ts = time.monotonic()

    def _on_sport_state(self, message: Any) -> None:
        """Cache the latest LF_SPORT_MOD_STATE; log only on summary changes.

        Upstream's sportmodestate example reads ``message["data"]`` with
        fields ``mode``, ``progress``, ``gait_type``, ``position``,
        ``velocity``, ``yaw_speed``, ``range_obstacle``, ``foot_force``,
        and IMU. We log mode + gait_type transitions; ``progress`` ticks
        rapidly and would flood. Phase 2 will gate move() on mode.
        """
        try:
            data = message.get("data") if isinstance(message, dict) else None
        except Exception:  # noqa: BLE001
            data = None
        if not isinstance(data, dict):
            return
        self._sport_state = data
        self._sport_state_ts = time.monotonic()
        summary = (data.get("mode"), data.get("gait_type"))
        if summary != self._sport_state_summary:
            log.info("sport state: mode=%s gait=%s", summary[0], summary[1])
            self._sport_state_summary = summary

    async def _force_motion_mode(self, target: str) -> None:
        """Best-effort switch to a named motion mode (e.g. "normal", "mcf").

        Mirrors upstream's `sportmode.py` startup: query the current mode,
        switch if it doesn't match, then wait for the controller to settle.
        Failures are logged but never block startup - if the firmware
        doesn't expose MOTION_SWITCHER, sport commands may still work.
        """
        if self._pubsub is None or not self._motion_switcher_topic:
            log.warning("motion switcher not available; skipping force_motion_mode")
            return
        try:
            current = await asyncio.wait_for(
                self._pubsub.publish_request_new(
                    self._motion_switcher_topic,
                    {"api_id": _MOTION_SWITCHER_CHECK_API},
                ),
                timeout=3.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("motion mode query failed: %s", exc)
            current = None

        current_name = _extract_motion_mode_name(current)
        if current_name == target:
            log.info("motion mode already %r; no switch needed", target)
            return

        log.info("switching motion mode: %r -> %r", current_name, target)
        try:
            await asyncio.wait_for(
                self._pubsub.publish_request_new(
                    self._motion_switcher_topic,
                    {
                        "api_id": _MOTION_SWITCHER_SET_API,
                        "parameter": {"name": target},
                    },
                ),
                timeout=3.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("motion mode set failed: %s", exc)
            return
        # Upstream sleeps ~5 s after switching before issuing sport commands.
        await asyncio.sleep(_MOTION_MODE_SETTLE_S)


# ----------------------------------------------------------------- helpers

def _request_msg_type() -> str:
    """Return the DATA_CHANNEL_TYPE['REQUEST'] string for the installed package."""
    from unitree_webrtc_connect import DATA_CHANNEL_TYPE  # type: ignore

    return DATA_CHANNEL_TYPE["REQUEST"]


def _extract_motion_mode_name(response: Any) -> Optional[str]:
    """Pull the current mode name out of a MOTION_SWITCHER query response.

    Upstream's example reads ``response["data"]["name"]``. The reply may
    arrive as a dict or as a pydantic-ish object; we accept either.
    """
    import json as _json

    if response is None:
        return None
    if isinstance(response, dict):
        data = response.get("data")
    else:
        data = getattr(response, "data", None)
    # The "data" field is sometimes a JSON-encoded string.
    if isinstance(data, str):
        try:
            data = _json.loads(data)
        except _json.JSONDecodeError:
            return None
    if isinstance(data, dict):
        name = data.get("name")
        if isinstance(name, str):
            return name
    return None


def _build_sport_request(api_id: int, parameter: dict[str, Any]) -> dict[str, Any]:
    """Build the JSON body the firmware expects for a sport request.

    Mirrors what ``WebRTCDataChannelPubSub.publish_request_new`` does, but
    we build it ourselves so we can send via ``publish_without_callback``
    (no per-frame round-trip during the 20 Hz move loop).
    """
    import json
    import random
    import time as _t

    generated_id = int(_t.time() * 1000) % 2147483648 + random.randint(0, 1000)
    return {
        "header": {
            "identity": {"id": generated_id, "api_id": api_id},
        },
        "parameter": json.dumps(parameter),
    }
