"""Thin async wrapper around unitree_webrtc_connect for the Go2 Air."""

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

_ADVANCED_ACTIONS: dict[str, list[str]] = {
    "greet": ["Hello"],
    "hello": ["Hello"],
    "dance": ["Dance1", "Dance2", "WiggleHips"],
    "dance1": ["Dance1"],
    "dance2": ["Dance2"],
    "jump": ["FrontJump", "FreeJump"],
    "pounce": ["FrontPounce", "Pounce"],
    "stretch": ["Stretch"],
    "wiggle": ["WiggleHips"],
    "heart": ["FingerHeart", "Heart"],
    "bound": ["Bound", "FreeBound"],
    "handstand": ["Handstand", "HandStand"],
    "backstand": ["BackStand"],
    "moonwalk": ["MoonWalk"],
    "wallow": ["Wallow"],
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
        self._pubsub = self._find_pubsub(self._conn)
        if self._pubsub is None:
            raise RuntimeError("WebRTC data channel pub/sub interface not found")
        await self._await_datachannel_ready()

        self._sport_topic = RTC_TOPIC["SPORT_MOD"]
        self._sport_state_topic = RTC_TOPIC.get("LF_SPORT_MOD_STATE") or RTC_TOPIC.get("SPORT_MOD_STATE")
        self._motion_switcher_topic = RTC_TOPIC.get("MOTION_SWITCHER")
        self._sport_cmd = dict(SPORT_CMD)

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
        await self._sport_request_first(["RecoveryStand", "RecoveryStandUp"])
        self._touch()

    async def sit_down(self) -> None:
        await self._sport_request_first(["StandDown", "Sit"])
        self._touch()

    async def advanced_action(self, name: str) -> None:
        key = _normalize_action_name(name)
        candidates = _ADVANCED_ACTIONS.get(key)
        if not candidates:
            raise RuntimeError(f"unknown advanced action {name!r}")
        await self._sport_request_first(candidates)
        self._touch()

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
        self._pubsub.publish_without_callback(self._sport_topic, payload, _request_msg_type())

    async def _send_stop_move(self) -> None:
        api_id = self._sport_cmd.get("StopMove")
        if api_id is None:
            self._publish_move(0.0, 0.0, 0.0)
            return
        async with self._lock:
            await self._pubsub.publish_request_new(self._sport_topic, {"api_id": api_id})

    async def _sport_request(self, cmd_name: str) -> None:
        api_id = self._sport_cmd.get(cmd_name)
        if api_id is None:
            raise RuntimeError(f"SPORT_CMD[{cmd_name!r}] missing from package")
        async with self._lock:
            await self._pubsub.publish_request_new(self._sport_topic, {"api_id": api_id})

    async def _sport_request_first(self, candidates: list[str]) -> None:
        for name in candidates:
            if name in self._sport_cmd:
                await self._sport_request(name)
                return
        raise RuntimeError(f"none of {candidates} are in SPORT_CMD")

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
        "header": {"identity": {"id": generated_id, "api_id": api_id}},
        "parameter": json.dumps(parameter),
    }
