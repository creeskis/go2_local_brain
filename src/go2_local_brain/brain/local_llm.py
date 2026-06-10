"""Local LLM brain: turn typed prompts into robot tool calls."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any, Awaitable, Callable

import ollama

from ..driver.webrtc_client import Go2WebRTCClient
from ..safety.limits import DEFAULT_MOVE_DURATION_S, MAX_MOVE_DURATION_S

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are the motion brain for a Unitree Go2 Air quadruped robot.\n"
    "Safety first: the robot has limited obstacle sensing. Prefer named "
    "primitive tools (robot_step_forward, robot_turn_left, robot_explore_room, "
    "etc.) over raw robot_move so duration and velocity stay conservative. "
    "When you do use robot_move directly, units are m/s for vx/vy and "
    "rad/s for vyaw; keep |vx| <= 0.5, |vy| <= 0.3, |vyaw| <= 1.0, and "
    "duration_s <= 1.0 unless the user explicitly asks for a long move. "
    "Never fabricate values larger than the user asked for.\n\n"
    "Roaming and patrolling: when the user asks the robot to roam, patrol, "
    "wander, explore, look around, or check the room, call robot_explore_room. "
    "Pick mode='telemetry' if obstacle data is available (LiDAR + range_obstacle "
    "feed the explore loop), mode='relaxed' if telemetry is partial, mode='blind' "
    "only when the operator explicitly says the area is clear. Default duration "
    "is around 8-15 seconds; never longer than 30s without an explicit request.\n\n"
    "Choose exactly ONE tool call for each user message. If the user asks for "
    "multiple actions using words like then, after, comma-separated steps, or "
    "several lines, you MUST use robot_sequence rather than only the first action. "
    "Use simple sequence cmd values, not tool names: forward, back, strafe_left, "
    "strafe_right, turn_left, turn_right, turn_90_left, turn_90_right, "
    "walk_turn_left, walk_turn_right, turn_180_left, turn_180_right, greet, "
    "dance, jump, pounce, stretch, wiggle, handstand, backstand, pause, stop. "
    "The driver can tolerate aliases, but prefer these exact strings.\n\n"
    "If the user asks for hind legs, back legs, rear legs, stand on two legs, "
    "or stand upright, choose robot_backstand. If the user explicitly asks for "
    "handstand/front-leg stunt, choose robot_handstand. Do not map those requests "
    "to robot_stand_up or robot_balance_stand.\n\n"
    "Use robot_explore_room when the user asks the robot to explore; mode can be "
    "telemetry, relaxed, or blind. Use robot_telemetry_report to inspect what "
    "telemetry the app currently sees. If the user asks for a full turnaround, "
    "call robot_turn_180 unless it is part of a multi-step request, in which case "
    "use robot_sequence. Do not invent tools.\n\n"
    "Examples:\n"
    '  user: "stand on your hind legs" -> robot_backstand()\n'
    '  user: "stand on your back legs" -> robot_backstand()\n'
    '  user: "do a handstand" -> robot_handstand()\n'
    '  user: "turn around" -> robot_turn_180(direction="left")\n'
    '  user: "turn right 90 degrees, then walk forward" -> robot_sequence(steps=[{"cmd":"turn_90_right"},{"cmd":"forward"}])\n'
    '  user: "jump, then walk forward" -> robot_sequence(steps=[{"cmd":"jump"},{"cmd":"forward"}])\n'
    '  user: "make up a dance" -> robot_dance_move(style="hype")\n'
    '  user: "explore even without telemetry" -> robot_explore_room(duration_s=8, mode="blind")\n'
    '  user: "roam the room" -> robot_explore_room(duration_s=15, mode="telemetry")\n'
    '  user: "patrol for 10 seconds" -> robot_explore_room(duration_s=10, mode="telemetry")\n'
    '  user: "wander around carefully" -> robot_explore_room(duration_s=12, mode="relaxed")\n'
    '  user: "why is obstacle data missing" -> robot_telemetry_report()\n'
    '  user: "stop" -> robot_stop()\n'
)


def _empty_tool(name: str, description: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _empty_tool("robot_stand_up", "Make the robot stand up, then enter BalanceStand."),
    _empty_tool("robot_balance_stand", "Put the robot in active balance mode."),
    _empty_tool("robot_recovery_stand", "Attempt the firmware recovery stand behavior."),
    _empty_tool("robot_sit_down", "Make the robot sit / lie down."),
    _empty_tool("robot_stop", "Immediately stop all motion."),
    _empty_tool("robot_step_forward", "Take a fast short step forward."),
    _empty_tool("robot_step_back", "Take a short step backward."),
    _empty_tool("robot_strafe_left", "Move sideways to the robot's left."),
    _empty_tool("robot_strafe_right", "Move sideways to the robot's right."),
    _empty_tool("robot_turn_left", "Turn counter-clockwise in place."),
    _empty_tool("robot_turn_right", "Turn clockwise in place."),
    _empty_tool("robot_greet", "Run the Go2 greeting / hello action."),
    _empty_tool("robot_dance", "Run a firmware Go2 dance action."),
    _empty_tool("robot_jump", "Run a Go2 jump action if this firmware exposes one."),
    _empty_tool("robot_pounce", "Run a Go2 pounce action if this firmware exposes one."),
    _empty_tool("robot_stretch", "Run a Go2 stretch action."),
    _empty_tool("robot_wiggle", "Run a Go2 wiggle-hips action if exposed."),
    _empty_tool("robot_handstand", "Run the firmware Handstand / HandStand action."),
    _empty_tool("robot_backstand", "Run the firmware BackStand action for hind-leg upright behavior."),
    _empty_tool("robot_telemetry_report", "Report sport-state telemetry keys and obstacle status."),
    {
        "type": "function",
        "function": {
            "name": "robot_turn_180",
            "description": "Turn around approximately 180 degrees in place.",
            "parameters": {
                "type": "object",
                "properties": {"direction": {"type": "string", "enum": ["left", "right"]}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "robot_dance_move",
            "description": "Run a movement-based dance macro. Styles: hype, sway, spin.",
            "parameters": {
                "type": "object",
                "properties": {"style": {"type": "string", "enum": ["hype", "sway", "spin"]}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "robot_walk_turn",
            "description": "Walk and turn at the same time for a short duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vx": {"type": "number"},
                    "vyaw": {"type": "number"},
                    "duration_s": {"type": "number"},
                },
                "required": ["vx", "vyaw"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "robot_sequence",
            "description": "Execute up to 8 linked known movement/action commands. Use this for any then/comma/multi-step prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "cmd": {"type": "string"},
                                "duration_s": {"type": "number"},
                            },
                            "required": ["cmd"],
                        },
                    }
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "robot_move",
            "description": "Drive at a constant velocity for a short duration. Use named tools when possible.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vx": {"type": "number"},
                    "vy": {"type": "number"},
                    "vyaw": {"type": "number"},
                    "duration_s": {"type": "number"},
                },
                "required": ["vx"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "robot_explore_room",
            "description": "Explore with short forward/turn steps. Modes: telemetry, relaxed, blind.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_s": {"type": "number"},
                    "mode": {"type": "string", "enum": ["telemetry", "relaxed", "blind"]},
                },
                "required": [],
            },
        },
    },
]


class LocalRobotBrain:
    """Routes natural-language prompts through Ollama into robot tool calls."""

    def __init__(self, client: Go2WebRTCClient, model: str) -> None:
        self._client = client
        self._model = model
        self._tools: dict[str, Callable[..., Awaitable[Any]]] = {
            "robot_stand_up": self._tool_stand_up,
            "robot_balance_stand": self._tool_balance_stand,
            "robot_recovery_stand": self._tool_recovery_stand,
            "robot_sit_down": self._tool_sit_down,
            "robot_stop": self._tool_stop,
            "robot_move": self._tool_move,
            "robot_step_forward": self._tool_step_forward,
            "robot_step_back": self._tool_step_back,
            "robot_strafe_left": self._tool_strafe_left,
            "robot_strafe_right": self._tool_strafe_right,
            "robot_turn_left": self._tool_turn_left,
            "robot_turn_right": self._tool_turn_right,
            "robot_turn_180": self._tool_turn_180,
            "robot_walk_turn": self._tool_walk_turn,
            "robot_sequence": self._tool_sequence,
            "robot_greet": self._tool_greet,
            "robot_dance": self._tool_dance,
            "robot_dance_move": self._tool_dance_move,
            "robot_jump": self._tool_jump,
            "robot_pounce": self._tool_pounce,
            "robot_stretch": self._tool_stretch,
            "robot_wiggle": self._tool_wiggle,
            "robot_handstand": self._tool_handstand,
            "robot_backstand": self._tool_backstand,
            "robot_explore_room": self._tool_explore_room,
            "robot_telemetry_report": self._tool_telemetry_report,
        }

    async def _tool_stand_up(self, **_: Any) -> None:
        await self._client.stand_up()

    async def _tool_balance_stand(self, **_: Any) -> None:
        await self._client.balance_stand()

    async def _tool_recovery_stand(self, **_: Any) -> None:
        await self._client.recovery_stand()

    async def _tool_sit_down(self, **_: Any) -> None:
        await self._client.sit_down()

    async def _tool_stop(self, **_: Any) -> None:
        await self._client.stop()

    async def _tool_step_forward(self, **_: Any) -> None:
        await self._client.move(0.45, 0.0, 0.0, 0.65)

    async def _tool_step_back(self, **_: Any) -> None:
        await self._client.move(-0.30, 0.0, 0.0, 0.55)

    async def _tool_strafe_left(self, **_: Any) -> None:
        await self._client.move(0.0, 0.28, 0.0, 0.55)

    async def _tool_strafe_right(self, **_: Any) -> None:
        await self._client.move(0.0, -0.28, 0.0, 0.55)

    async def _tool_turn_left(self, **_: Any) -> None:
        await self._client.move(0.0, 0.0, 0.75, 0.55)

    async def _tool_turn_right(self, **_: Any) -> None:
        await self._client.move(0.0, 0.0, -0.75, 0.55)

    async def _tool_turn_180(self, direction: str = "left", **_: Any) -> None:
        await self._client.turn_180(direction)

    async def _tool_walk_turn(self, vx: float = 0.35, vyaw: float = 0.45, duration_s: float = DEFAULT_MOVE_DURATION_S, **_: Any) -> None:
        await self._tool_move(vx=vx, vy=0.0, vyaw=vyaw, duration_s=duration_s)

    async def _tool_sequence(self, steps: list[dict[str, Any]], **_: Any) -> None:
        if not isinstance(steps, list):
            raise ValueError("steps must be a list")
        await self._client.sequence(steps)

    async def _tool_greet(self, **_: Any) -> None:
        await self._client.advanced_action("greet")

    async def _tool_dance(self, **_: Any) -> None:
        await self._client.advanced_action("dance")

    async def _tool_dance_move(self, style: str = "hype", **_: Any) -> None:
        await self._client.dance_move(style)

    async def _tool_jump(self, **_: Any) -> None:
        await self._client.advanced_action("jump")

    async def _tool_pounce(self, **_: Any) -> None:
        await self._client.advanced_action("pounce")

    async def _tool_stretch(self, **_: Any) -> None:
        await self._client.advanced_action("stretch")

    async def _tool_wiggle(self, **_: Any) -> None:
        await self._client.advanced_action("wiggle")

    async def _tool_handstand(self, **_: Any) -> None:
        await self._client.advanced_action("handstand")

    async def _tool_backstand(self, **_: Any) -> None:
        await self._client.advanced_action("backstand")

    async def _tool_explore_room(self, duration_s: float = 5.0, mode: str | None = None, **_: Any) -> None:
        value = float(duration_s)
        if not math.isfinite(value):
            raise ValueError("duration_s must be finite")
        await self._client.explore_room(value, mode=mode)

    async def _tool_telemetry_report(self, **_: Any) -> str:
        return self._client.telemetry_report()

    async def _tool_move(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0, duration_s: float = DEFAULT_MOVE_DURATION_S, **_: Any) -> None:
        values = (float(vx), float(vy), float(vyaw), float(duration_s))
        if not all(math.isfinite(v) for v in values):
            raise ValueError("move arguments must be finite numbers")
        safe_duration = min(max(0.0, values[3]), MAX_MOVE_DURATION_S)
        await self._client.move(values[0], values[1], values[2], safe_duration)

    async def handle(self, user_text: str) -> str:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]

        try:
            response = await asyncio.to_thread(ollama.chat, model=self._model, messages=messages, tools=_TOOL_SCHEMAS)
        except Exception as exc:  # noqa: BLE001
            log.exception("ollama.chat failed: %s", exc)
            await self._client.stop()
            return f"ollama error -> stop: {exc}"

        tool_calls = _extract_tool_calls(response)
        if not tool_calls:
            await self._client.stop()
            return "no tool call returned -> stop"

        call = tool_calls[0]
        name = call.get("name")
        args = call.get("arguments") or {}

        fn = self._tools.get(name)
        if fn is None:
            await self._client.stop()
            return f"unknown tool {name!r} -> stop"

        try:
            result = await fn(**args)
        except TypeError as exc:
            await self._client.stop()
            return f"bad args for {name}: {exc} -> stop"
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            await self._client.stop()
            return f"tool {name} failed: {exc} -> stop"

        suffix = f" -> {result}" if result is not None else ""
        return f"called {name}({_format_args(args)}){suffix}"

    async def repl(self) -> None:
        print("Go2 local brain ready. Type a command, or 'quit' to exit.")
        while True:
            try:
                line = await asyncio.to_thread(input, "go2> ")
            except EOFError:
                break
            text = line.strip()
            if not text:
                continue
            if text.lower() in {"quit", "exit"}:
                break
            result = await self.handle(text)
            print(result)


def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    message = _get(response, "message")
    if message is None:
        return []
    raw_calls = _get(message, "tool_calls") or []
    out: list[dict[str, Any]] = []
    for call in raw_calls:
        function = _get(call, "function") or {}
        name = _get(function, "name")
        args = _get(function, "arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        if name:
            out.append({"name": name, "arguments": args})
    return out


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _format_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items())
