"""Thin async wrapper around unitree_webrtc_connect for the Go2 Air."""

from __future__ import annotations

import asyncio
import logging
import socket
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

_MOVE_REFRESH_HZ = 20.0
_MOVE_REFRESH_PERIOD = 1.0 / _MOVE_REFRESH_HZ
_DEADMAN_TICK_S = 0.1
_DC_OPEN_TIMEOUT_S = 15.0
_STAND_TO_BALANCE_PAUSE_S = 2.5
_MOTION_SWITCHER_CHECK_API = 1001
_MOTION_SWITCHER_SET_API = 1002
_MOTION_MODE_SETTLE_S = 5.0

_SPORT_STATE_STALE_S = 1.0
_EXPLORE_STEP_S = 0.35
_EXPLORE_TURN_S = 0.45
_EXPLORE_VX = 0.30
_EXPLORE_VYAW = 0.80
_TURN_180_VYAW = 1.00
_TURN_180_DURATION_S = 3.20
_MAX_SEQUENCE_STEPS = 8

_ADVANCED_ACTIONS: dict[str, list[tuple[str, Optional[dict[str, Any]]]]] = {
    "greet": [("Hello", None)],
    "hello": [("Hello", None)],
    "dance": [("Dance1", None), ("Dance2", None), ("WiggleHips", None)],
    "dance1": [("Dance1", None)],
    "dance2": [("Dance2", None)],
    "jump": [("FrontJump", None), ("FreeJump", {"data": True})],
    "pounce": [("FrontPounce", None), ("Pounce", None)],
    "stretch": [("Stretch", None)],
    "wiggle": [("WiggleHips", None)],
    "heart": [("Heart", None), ("FingerHeart", None)],
    "bound": [("FreeBound", {"data": True}), ("Bound", None)],
    "handstand": [("HandStand", {"data": True}), ("Handstand", None)],
    "backstand": [("BackStand", {"data": True})],
    # Directional flips. MCF table (1.1.7) ids differ from base; the driver's
    # _sport_request_first tries MCF first then base, so listing the command
    # name once resolves in whichever table the SDK exposes. RightFlip is
    # base-only on this SDK, so it falls back to that automatically.
    "front_flip": [("FrontFlip", {"data": True})],
    "back_flip": [("BackFlip", {"data": True})],
    "left_flip": [("LeftFlip", {"data": True})],
    "right_flip": [("RightFlip", {"data": True})],
    "moonwalk": [("MoonWalk", None)],
    "wallow": [("Wallow", None)],
}

_SEQUENCE_ALIASES: dict[str, str] = {
    "forward": "forward",
    "walkforward": "forward",
    "stepforward": "forward",
    "robotstepforward": "forward",
    "robotwalkforward": "forward",
    "robotforward": "forward",
    "back": "back",
    "backward": "back",
    "walkback": "back",
    "stepback": "back",
    "robotstepback": "back",
    "left": "strafe_left",
    "strafeleft": "strafe_left",
    "robotstrafeleft": "strafe_left",
    "right": "strafe_right",
    "straferight": "strafe_right",
    "robotstraferight": "strafe_right",
    "turnleft": "turn_left",
    "robotturnleft": "turn_left",
    "turnleft90": "turn_90_left",
    "robotturnleft90": "turn_90_left",
    "turnright": "turn_right",
    "robotturnright": "turn_right",
    "turnright90": "turn_90_right",
    "robotturnright90": "turn_90_right",
    "walkturnleft": "walk_turn_left",
    "robotwalkturnleft": "walk_turn_left",
    "walkturnright": "walk_turn_right",
    "robotwalkturnright": "walk_turn_right",
    "turn180left": "turn_180_left",
    "turnaroundleft": "turn_180_left",
    "robotturn180left": "turn_180_left",
    "turn180right": "turn_180_right",
    "turnaroundright": "turn_180_right",
    "robotturn180right": "turn_180_right",
    "handstand": "handstand",
    "handstandaction": "handstand",
    "robothandstand": "handstand",
    "backstand": "backstand",
    "backstandaction": "backstand",
    "robotbackstand": "backstand",
    "hindlegs": "backstand",
    "backlegs": "backstand",
    "standontwolegs": "backstand",
    "pause": "pause",
    "wait": "pause",
    "stop": "stop",
    "robotstop": "stop",
}


@dataclass
class Go2Config:
    """Connection parameters for the robot."""

    ip: str
    aes_128_key: Optional[str] = None
    webrtc_method: str = "LocalSTA"
    serial_number: Optional[str] = None
    remote_username: Optional[str] = None
    remote_password: Optional[str] = None
    remote_region: str = "global"
    remote_device_type: str = "Go2"
    force_motion_mode: Optional[str] = None
    enable_exploration: bool = False
    exploration_min_obstacle_m: float = 0.35
    exploration_mode: str = "telemetry"
    exploration_max_duration_s: float = 15.0


class Go2WebRTCClient:
    """Async-friendly wrapper around ``UnitreeWebRTCConnection``."""

    def __init__(self, cfg: Go2Config) -> None:
        self._cfg = cfg
        self._conn: Any = None
        self._pubsub: Any = None
        self._sport_topic: Optional[str] = None
        self._sport_state_topic: Optional[str] = None
        self._motion_switcher_topic: Optional[str] = None
        self._sport_cmd: dict[str, int] = {}
        self._sport_cmd_mcf: dict[str, int] = {}

        self._last_cmd_ts: float = 0.0
        self._deadman_task: Optional[asyncio.Task[None]] = None
        self._move_task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()

        self._sport_state: dict[str, Any] = {}
        self._sport_state_ts: float = 0.0
        self._sport_state_summary: Optional[tuple[Any, Any]] = None

    async def connect(self) -> None:
        """Open WebRTC, wait for the data channel, and cache the sport topic."""
        from unitree_webrtc_connect import (  # type: ignore
            RTC_TOPIC,
            SPORT_CMD,
            UnitreeWebRTCConnection,
            WebRTCConnectionMethod,
        )
        try:
            from unitree_webrtc_connect import SPORT_CMD_MCF  # type: ignore
        except ImportError:
            SPORT_CMD_MCF = {}

        method = _resolve_webrtc_method(WebRTCConnectionMethod, self._cfg.webrtc_method)
        kwargs = self._connection_kwargs(method)
        if self._cfg.aes_128_key:
            kwargs["aes_128_key"] = self._cfg.aes_128_key
        self._log_connection_plan(method, kwargs)

        self._conn = _build_unitree_connection(UnitreeWebRTCConnection, method, kwargs, self._cfg.aes_128_key)

        try:
            await self._conn.connect()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(_friendly_connect_error(exc, self._cfg, method)) from exc
        self._pubsub = self._find_pubsub(self._conn)
        if self._pubsub is None:
            raise RuntimeError("WebRTC data channel pub/sub interface not found")
        await self._await_datachannel_ready()

        self._sport_topic = RTC_TOPIC["SPORT_MOD"]
        self._sport_state_topic = RTC_TOPIC.get("LF_SPORT_MOD_STATE") or RTC_TOPIC.get("SPORT_MOD_STATE")
        self._motion_switcher_topic = RTC_TOPIC.get("MOTION_SWITCHER")
        self._sport_cmd = dict(SPORT_CMD)
        self._sport_cmd_mcf = dict(SPORT_CMD_MCF)
        if "BackStand" in self._sport_cmd:
            log.info("advanced MCF sport actions available")
        elif "BackStand" in self._sport_cmd_mcf:
            log.info("advanced MCF sport actions available")

        if self._cfg.force_motion_mode:
            await self._force_motion_mode(self._cfg.force_motion_mode)

        if self._sport_state_topic:
            try:
                self._pubsub.subscribe(self._sport_state_topic, self._on_sport_state)
            except Exception as exc:  # noqa: BLE001
                log.warning("sport state subscribe failed: %s", exc)

        log.info("Go2 WebRTC connected at %s", self._cfg.ip)
        self._last_cmd_ts = time.monotonic()
        self._deadman_task = asyncio.create_task(self._deadman_loop(), name="go2-deadman")

    def _connection_kwargs(self, method: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"connectionMethod": method}
        method_name = _method_name(method)
        if method_name == "LocalAP":
            return kwargs
        if method_name == "Remote":
            if self._cfg.serial_number:
                kwargs["serialNumber"] = self._cfg.serial_number
            if self._cfg.remote_username:
                kwargs["username"] = self._cfg.remote_username
            if self._cfg.remote_password:
                kwargs["password"] = self._cfg.remote_password
            kwargs["region"] = self._cfg.remote_region
            kwargs["device_type"] = self._cfg.remote_device_type
            return kwargs
        if self._cfg.ip:
            kwargs["ip"] = self._cfg.ip
        if self._cfg.serial_number:
            kwargs["serialNumber"] = self._cfg.serial_number
        return kwargs

    def _log_connection_plan(self, method: Any, kwargs: dict[str, Any]) -> None:
        method_name = _method_name(method)
        local_ip = _local_ip_for_target(self._cfg.ip) if self._cfg.ip else None
        if method_name == "LocalSTA":
            signaling = f"http://{self._cfg.ip}:9991/con_notify or :8081 fallback"
        elif method_name == "LocalAP":
            signaling = "robot AP default signaling"
        else:
            signaling = f"Unitree remote TURN flow region={self._cfg.remote_region}"
        redacted = {
            key: ("***" if key in {"password", "aes_128_key", "aesKey"} else value)
            for key, value in kwargs.items()
            if key != "connectionMethod"
        }
        log.info(
            "Go2 WebRTC connection plan: method=%s target_ip=%s signaling=%s aes_key=%s local_ip=%s args=%s",
            method_name,
            self._cfg.ip or "none",
            signaling,
            "present" if self._cfg.aes_128_key else "blank",
            local_ip or "unknown",
            redacted,
        )

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

    async def stand_up(self) -> None:
        await self._sport_request("StandUp")
        if "BalanceStand" in self._sport_cmd:
            await asyncio.sleep(_STAND_TO_BALANCE_PAUSE_S)
            await self._sport_request("BalanceStand")
        self._touch()

    async def balance_stand(self) -> None:
        await self._sport_request("BalanceStand")
        self._touch()

    async def recovery_stand(self) -> None:
        await self._sport_request_first(_action_candidates("RecoveryStand", "RecoveryStandUp"))
        self._touch()

    async def sit_down(self) -> None:
        await self._sport_request_first(_action_candidates("StandDown", "Sit"))
        self._touch()

    async def advanced_action(self, name: str) -> None:
        key = _normalize_action_name(name)
        candidates = _ADVANCED_ACTIONS.get(key)
        if not candidates:
            raise RuntimeError(f"unknown advanced action {name!r}")
        await self._sport_request_first(candidates)
        self._touch()

    async def sport_command(self, cmd_name: str, parameter: Optional[dict[str, Any]] = None) -> None:
        """Run an exact SPORT_CMD / SPORT_CMD_MCF command exposed by the installed SDK."""
        await self._sport_request_first([(cmd_name, parameter)])
        self._touch()

    async def sport_command_response(self, cmd_name: str, parameter: Optional[dict[str, Any]] = None) -> Any:
        """Run an exact sport command and return the robot response for diagnostics."""
        response = await self._sport_request_first([(cmd_name, parameter)])
        self._touch()
        return response

    async def motion_mode_status(self) -> Any:
        """Return the raw motion-switcher status response, if that topic is available."""
        if self._pubsub is None or not self._motion_switcher_topic:
            raise RuntimeError("motion switcher topic is not available")
        return await asyncio.wait_for(
            self._pubsub.publish_request_new(self._motion_switcher_topic, {"api_id": _MOTION_SWITCHER_CHECK_API}),
            timeout=3.0,
        )

    async def set_motion_mode(self, target: str) -> Any:
        """Set motion-switcher mode and return the raw response."""
        if self._pubsub is None or not self._motion_switcher_topic:
            raise RuntimeError("motion switcher topic is not available")
        response = await asyncio.wait_for(
            self._pubsub.publish_request_new(
                self._motion_switcher_topic,
                {"api_id": _MOTION_SWITCHER_SET_API, "parameter": {"name": target}},
            ),
            timeout=3.0,
        )
        await asyncio.sleep(_MOTION_MODE_SETTLE_S)
        return response

    def available_sport_commands(self) -> list[str]:
        """Return exact sport command names available from the installed package."""
        return sorted(set(self._sport_cmd) | set(self._sport_cmd_mcf))

    async def turn_180(self, direction: str = "left") -> None:
        """Turn around approximately 180 degrees in place."""
        sign = -1.0 if direction.strip().lower() in {"right", "clockwise", "cw"} else 1.0
        await self.move(0.0, 0.0, sign * _TURN_180_VYAW, _TURN_180_DURATION_S)

    async def turn_degrees(self, direction: str, degrees: float) -> None:
        """Turn an approximate number of degrees by scaling the tested 180 turn."""
        clamped_degrees = min(max(abs(float(degrees)), 15.0), 360.0)
        sign = -1.0 if direction.strip().lower() in {"right", "clockwise", "cw"} else 1.0
        duration = _TURN_180_DURATION_S * (clamped_degrees / 180.0)
        await self.move(0.0, 0.0, sign * _TURN_180_VYAW, duration)

    async def dance_move(self, style: str = "hype") -> None:
        """Run a movement-based dance macro, with firmware gestures when present."""
        key = _normalize_action_name(style)
        if key in {"spin", "turn"}:
            await self.sequence([
                {"cmd": "turn_left", "duration_s": 0.65},
                {"cmd": "turn_right", "duration_s": 0.65},
                {"cmd": "turn_180_left"},
            ])
        elif key in {"sway", "side"}:
            await self.sequence([
                {"cmd": "strafe_left", "duration_s": 0.45},
                {"cmd": "strafe_right", "duration_s": 0.45},
                {"cmd": "strafe_right", "duration_s": 0.45},
                {"cmd": "strafe_left", "duration_s": 0.45},
            ])
        else:
            await self._try_advanced_action("dance")
            await self.sequence([
                {"cmd": "walk_turn_left", "duration_s": 0.55},
                {"cmd": "walk_turn_right", "duration_s": 0.55},
                {"cmd": "strafe_left", "duration_s": 0.35},
                {"cmd": "strafe_right", "duration_s": 0.35},
            ])

    async def sequence(self, steps: list[dict[str, Any]]) -> None:
        """Execute a short chain of known commands from one model tool call."""
        if not isinstance(steps, list) or not steps:
            raise RuntimeError("sequence requires at least one step")
        for step in steps[:_MAX_SEQUENCE_STEPS]:
            if not isinstance(step, dict):
                continue
            cmd = _canonical_sequence_cmd(str(step.get("cmd", "")))
            duration = float(step.get("duration_s", DEFAULT_MOVE_DURATION_S))
            duration = min(max(duration, 0.05), 2.5)
            await self._run_sequence_step(cmd, duration)
        await self.stop()

    async def _run_sequence_step(self, cmd: str, duration_s: float) -> None:
        if cmd == "forward":
            await self.move(0.45, 0.0, 0.0, duration_s)
        elif cmd == "back":
            await self.move(-0.32, 0.0, 0.0, duration_s)
        elif cmd == "strafe_left":
            await self.move(0.0, 0.30, 0.0, duration_s)
        elif cmd == "strafe_right":
            await self.move(0.0, -0.30, 0.0, duration_s)
        elif cmd == "turn_left":
            await self.move(0.0, 0.0, 0.85, duration_s)
        elif cmd == "turn_right":
            await self.move(0.0, 0.0, -0.85, duration_s)
        elif cmd == "turn_90_left":
            await self.turn_degrees("left", 90.0)
        elif cmd == "turn_90_right":
            await self.turn_degrees("right", 90.0)
        elif cmd == "walk_turn_left":
            await self.move(0.35, 0.0, 0.75, duration_s)
        elif cmd == "walk_turn_right":
            await self.move(0.35, 0.0, -0.75, duration_s)
        elif cmd == "turn_180_left":
            await self.turn_180("left")
        elif cmd == "turn_180_right":
            await self.turn_180("right")
        elif cmd in _ADVANCED_ACTIONS:
            await self._try_advanced_action(cmd)
        elif cmd == "pause":
            await asyncio.sleep(duration_s)
        elif cmd == "stop":
            await self.stop()
        else:
            raise RuntimeError(f"unknown sequence command {cmd!r}")

    async def _try_advanced_action(self, name: str) -> None:
        try:
            await self.advanced_action(name)
        except Exception as exc:  # noqa: BLE001
            log.info("advanced action %s unavailable: %s", name, exc)

    async def stop(self) -> None:
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
        cvx = clamp(vx, -MAX_VX, MAX_VX)
        cvy = clamp(vy, -MAX_VY, MAX_VY)
        cvyaw = clamp(vyaw, -MAX_VYAW, MAX_VYAW)
        dur = max(0.0, float(duration_s))
        if (cvx, cvy, cvyaw) != (vx, vy, vyaw):
            log.info(
                "move clamped: requested vx=%.3f vy=%.3f vyaw=%.3f -> vx=%.3f vy=%.3f vyaw=%.3f",
                vx,
                vy,
                vyaw,
                cvx,
                cvy,
                cvyaw,
            )

        await self._cancel_move_task()
        self._move_task = asyncio.create_task(self._move_loop(cvx, cvy, cvyaw, dur), name="go2-move")
        try:
            await self._move_task
        except asyncio.CancelledError:
            pass

    async def explore_room(self, duration_s: float = 3.0, mode: Optional[str] = None) -> None:
        """Explore with short forward/turn steps."""
        if not self._cfg.enable_exploration:
            raise RuntimeError("exploration is disabled; set ENABLE_EXPLORATION=1 to opt in")
        active_mode = (mode or self._cfg.exploration_mode).strip().lower()
        if active_mode not in {"telemetry", "relaxed", "blind"}:
            active_mode = self._cfg.exploration_mode
        max_duration = min(max(0.0, float(duration_s)), self._cfg.exploration_max_duration_s)
        if max_duration <= 0.0:
            return

        if active_mode == "telemetry" and self._valid_range_obstacles() is None:
            raise RuntimeError("range_obstacle telemetry is unavailable; use EXPLORATION_MODE=relaxed or blind to override")

        deadline = time.monotonic() + max_duration
        turn_sign = 1.0
        step_count = 0
        while time.monotonic() < deadline:
            ranges = self._valid_range_obstacles()
            if active_mode == "telemetry" and ranges is None:
                raise RuntimeError("range_obstacle telemetry went stale during exploration")

            if active_mode == "blind" or ranges is None:
                turn_sign = -turn_sign if step_count % 4 == 3 else turn_sign
                await self.move(_EXPLORE_VX, 0.0, 0.25 * turn_sign, _EXPLORE_STEP_S)
            else:
                front = ranges[0]
                closest = min(ranges)
                if front > self._cfg.exploration_min_obstacle_m and closest > self._cfg.exploration_min_obstacle_m * 0.65:
                    await self.move(_EXPLORE_VX, 0.0, 0.10 * turn_sign, _EXPLORE_STEP_S)
                else:
                    turn_sign = self._pick_explore_turn(ranges, turn_sign)
                    await self.move(0.0, 0.0, _EXPLORE_VYAW * turn_sign, _EXPLORE_TURN_S)
            step_count += 1
        await self.stop()

    def telemetry_report(self) -> str:
        age = time.monotonic() - self._sport_state_ts if self._sport_state_ts else None
        ranges = self._valid_range_obstacles()
        keys = sorted(self._sport_state.keys()) if self._sport_state else []
        raw_ranges = self._sport_state.get("range_obstacle") if self._sport_state else None
        age_text = "none" if age is None else f"{age:.2f}s"
        range_text = "usable" if ranges is not None else "unavailable"
        return (
            f"sport_state_age={age_text}; keys={keys}; "
            f"range_obstacle={raw_ranges}; range_status={range_text}; "
            "if range_obstacle is all zeros, use lowstate/lidar/obstacle API diagnostics next"
        )

    async def _move_loop(self, vx: float, vy: float, vyaw: float, duration_s: float) -> None:
        deadline = time.monotonic() + duration_s
        try:
            while time.monotonic() < deadline:
                self._publish_move(vx, vy, vyaw)
                self._touch()
                remaining = deadline - time.monotonic()
                await asyncio.sleep(min(_MOVE_REFRESH_PERIOD, max(0.0, remaining)))
        finally:
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
        try:
            while True:
                await asyncio.sleep(_DEADMAN_TICK_S)
                age = time.monotonic() - self._last_cmd_ts
                if age > DEADMAN_TIMEOUT_S:
                    try:
                        self._publish_move(0.0, 0.0, 0.0)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("deadman zero-vel send failed: %s", exc)
                    self._last_cmd_ts = time.monotonic()
        except asyncio.CancelledError:
            return

    def _publish_move(self, vx: float, vy: float, vyaw: float) -> None:
        if self._pubsub is None or self._sport_topic is None:
            raise RuntimeError("not connected: call connect() first")
        channel = getattr(self._pubsub, "channel", None)
        if channel is not None and getattr(channel, "readyState", None) != "open":
            raise RuntimeError("data channel is not open")
        api_id = self._sport_cmd.get("Move")
        if api_id is None:
            raise RuntimeError("SPORT_CMD['Move'] missing from package")
        payload = _build_sport_request(api_id, {"x": float(vx), "y": float(vy), "z": float(vyaw)})
        self._pubsub.publish_without_callback(self._sport_topic, payload)

    async def _send_stop_move(self) -> None:
        api_id = self._sport_cmd.get("StopMove")
        if api_id is None:
            self._publish_move(0.0, 0.0, 0.0)
            return
        async with self._lock:
            await self._pubsub.publish_request_new(self._sport_topic, {"api_id": api_id})

    async def _sport_request(self, cmd_name: str, parameter: Optional[dict[str, Any]] = None, *, mcf: bool = False) -> Any:
        table = self._sport_cmd_mcf if mcf else self._sport_cmd
        api_id = table.get(cmd_name)
        if api_id is None:
            raise RuntimeError(f"SPORT_CMD[{cmd_name!r}] missing from package")
        payload = {"api_id": api_id}
        if parameter is not None:
            payload["parameter"] = parameter
        async with self._lock:
            return await self._pubsub.publish_request_new(self._sport_topic, payload)

    async def _sport_request_first(self, candidates: list[tuple[str, Optional[dict[str, Any]]]]) -> Any:
        for name, parameter in candidates:
            if name in self._sport_cmd_mcf:
                return await self._sport_request(name, parameter, mcf=True)
            if name in self._sport_cmd:
                return await self._sport_request(name, parameter)
        names = [name for name, _parameter in candidates]
        raise RuntimeError(f"none of {names} are in SPORT_CMD or SPORT_CMD_MCF")

    def _valid_range_obstacles(self) -> Optional[list[float]]:
        if not self._sport_state or time.monotonic() - self._sport_state_ts > _SPORT_STATE_STALE_S:
            return None
        raw = self._sport_state.get("range_obstacle")
        if not isinstance(raw, list) or not raw:
            return None
        try:
            values = [float(v) for v in raw]
        except (TypeError, ValueError):
            return None
        if all(v <= 0.0 for v in values):
            return None
        return values

    @staticmethod
    def _pick_explore_turn(ranges: list[float], previous: float) -> float:
        if len(ranges) >= 3:
            left = ranges[1]
            right = ranges[2]
            if left > right:
                return 1.0
            if right > left:
                return -1.0
        return previous if previous in {-1.0, 1.0} else 1.0

    @staticmethod
    def _find_pubsub(conn: Any) -> Any:
        dc = getattr(conn, "datachannel", None) or getattr(conn, "data_channel", None)
        if dc is None:
            return None
        return getattr(dc, "pub_sub", None) or getattr(dc, "pubsub", None) or getattr(dc, "publisher", None)

    async def _await_datachannel_ready(self) -> None:
        dc = getattr(self._conn, "datachannel", None) or getattr(self._conn, "data_channel", None)
        if dc is None:
            return
        waiter = getattr(dc, "wait_datachannel_open", None)
        if callable(waiter):
            try:
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
        self._last_cmd_ts = time.monotonic()

    def _on_sport_state(self, message: Any) -> None:
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
        if self._pubsub is None or not self._motion_switcher_topic:
            log.warning("motion switcher not available; skipping force_motion_mode")
            return
        try:
            current = await asyncio.wait_for(
                self._pubsub.publish_request_new(self._motion_switcher_topic, {"api_id": _MOTION_SWITCHER_CHECK_API}),
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
                    {"api_id": _MOTION_SWITCHER_SET_API, "parameter": {"name": target}},
                ),
                timeout=3.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("motion mode set failed: %s", exc)
            return
        await asyncio.sleep(_MOTION_MODE_SETTLE_S)


def _merge_sport_cmds(base: dict[str, int], *extras: dict[str, int]) -> dict[str, int]:
    """Merge optional Unitree command tables without changing known base IDs."""
    merged = dict(base)
    for table in extras:
        for name, api_id in dict(table).items():
            merged.setdefault(name, api_id)
    return merged


def _build_unitree_connection(factory: Any, method: Any, kwargs: dict[str, Any], aes_key: str | None = None) -> Any:
    """Build Unitree connection using the positional method form shown upstream."""
    positional_kwargs = {key: value for key, value in kwargs.items() if key != "connectionMethod"}
    attempts: list[tuple[str, dict[str, Any], bool]] = [
        ("positional_aes_128_key", dict(positional_kwargs), True),
        ("keyword_aes_128_key", dict(kwargs), True),
        ("positional_aesKey", dict(positional_kwargs), False),
        ("keyword_aesKey", dict(kwargs), False),
    ]
    last_error: TypeError | None = None
    for _name, attempt_kwargs, use_modern_key in attempts:
        if aes_key:
            attempt_kwargs["aes_128_key" if use_modern_key else "aesKey"] = aes_key
        try:
            if "positional" in _name:
                return factory(method, **attempt_kwargs)
            return factory(**attempt_kwargs)
        except TypeError as exc:
            last_error = exc
            continue
    assert last_error is not None
    raise last_error


def _resolve_webrtc_method(enum: Any, requested: str) -> Any:
    """Resolve a user-facing method name against the SDK enum."""
    normalized = requested.strip().replace("-", "").replace("_", "").lower() or "localsta"
    aliases = {
        "sta": "LocalSTA",
        "localsta": "LocalSTA",
        "stal": "LocalSTA",
        "local": "LocalSTA",
        "ap": "LocalAP",
        "localap": "LocalAP",
        "remote": "Remote",
        "stat": "Remote",
    }
    target = aliases.get(normalized, requested.strip())
    for name in dir(enum):
        if name.startswith("_"):
            continue
        if name.lower() == target.lower():
            return getattr(enum, name)
    available = [name for name in dir(enum) if not name.startswith("_")]
    raise RuntimeError(f"unknown GO2_WEBRTC_METHOD={requested!r}; available methods: {available}")


def _method_name(method: Any) -> str:
    name = getattr(method, "name", None)
    if isinstance(name, str):
        return name
    text = str(method)
    return text.rsplit(".", 1)[-1]


def _local_ip_for_target(target_ip: str) -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((target_ip, 9991))
            return str(sock.getsockname()[0])
    except OSError:
        return None


def _friendly_connect_error(exc: Exception, cfg: Go2Config, method: Any) -> str:
    exc_name = type(exc).__name__
    method_name = _method_name(method)
    base = (
        f"Go2 WebRTC connect failed: method={method_name} target_ip={cfg.ip} "
        f"aes_key={'present' if cfg.aes_128_key else 'blank'} error={exc_name}: {exc}"
    )
    if exc_name == "NoSdpAnswerError" or "NoSdpAnswer" in exc_name:
        return (
            f"{base}. Robot signaling accepted the request but returned no SDP answer. "
            "On Go2 firmware 1.1.7 this usually means the robot WebRTC bridge is busy, wedged, "
            "or confused by interface/routing changes. Keep GO2_WEBRTC_METHOD=LocalSTA, target "
            "the robot wlan0 IP 192.168.123.121 first, stop other clients, then restart the robot "
            "WebRTC bridge or roll back dog-side NAT/iptables changes before retrying."
        )
    if exc_name == "RobotBusyError":
        return f"{base}. Another WebRTC client is probably connected; close phone apps/viewers and retry."
    if exc_name == "LocalSignalingPortError":
        return f"{base}. Neither local signaling port responded; check reachability to :9991 and :8081."
    if exc_name in {"AesKeyRequiredError", "AesKeyRejectedError"}:
        return f"{base}. Firmware requested AES authentication; set GO2_AES_128_KEY to the 32-hex key."
    return base


def _action_candidates(*names: str) -> list[tuple[str, Optional[dict[str, Any]]]]:
    return [(name, None) for name in names]


def _normalize_action_name(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def _compact_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _canonical_sequence_cmd(name: str) -> str:
    compact = _compact_name(name)
    if compact in _SEQUENCE_ALIASES:
        return _SEQUENCE_ALIASES[compact]
    if compact.startswith("robot"):
        compact = compact[5:]
        if compact in _SEQUENCE_ALIASES:
            return _SEQUENCE_ALIASES[compact]
    action_key = _normalize_action_name(name)
    if action_key in _ADVANCED_ACTIONS:
        return action_key
    compact_action = _normalize_action_name(compact)
    if compact_action in _ADVANCED_ACTIONS:
        return compact_action
    return name.strip().lower()


def _request_msg_type() -> str:
    from unitree_webrtc_connect import DATA_CHANNEL_TYPE  # type: ignore

    return DATA_CHANNEL_TYPE["REQUEST"]


def _extract_motion_mode_name(response: Any) -> Optional[str]:
    import json as _json

    if response is None:
        return None
    if isinstance(response, dict):
        data = response.get("data")
    else:
        data = getattr(response, "data", None)
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
    import json
    import random
    import time as _t

    generated_id = int(_t.time() * 1000) % 2147483648 + random.randint(0, 1000)
    return {
        "header": {
            "identity": {"id": generated_id, "api_id": api_id},
            "policy": {"priority": 0, "noreply": True},
        },
        "parameter": json.dumps(parameter),
        "binary": [],
    }
