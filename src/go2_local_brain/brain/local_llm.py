"""Local LLM brain: turn typed prompts into one robot tool call.

Why this shape
--------------
* ollama.chat() is synchronous and can take seconds - we run it in a worker
  thread via asyncio.to_thread() so WebRTC keeps flowing on the main loop.
* We deliberately execute only the *first* tool call. Multi-step plans are
  out of scope for the local brain; the operator can issue another prompt
  to get the next step.
* Unknown tool name or no tool call at all -> emit a stop(). Safer default
  than "ignore" because the LLM might have just hallucinated a verb.
"""

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
    "On every user instruction, choose exactly ONE available tool. Use named "
    "tools for postures, gestures, steps, turns, and exploration. Velocities "
    "are in m/s and rad/s. Positive vx is forward, positive vy is left, "
    "positive vyaw is counter-clockwise. The driver clamps hard limits, but "
    "you should still prefer short, intentional commands: duration_s <= 1.2 "
    "for normal movement and <= 6 for exploration. If the user wants a halt "
    "or you are unsure, call robot_stop. Do not invent tools.\n\n"
    "Examples:\n"
    '  user: "stand up"                 -> robot_stand_up()\n'
    '  user: "balance"                  -> robot_balance_stand()\n'
    '  user: "walk forward"             -> robot_step_forward()\n'
    '  user: "back up"                  -> robot_step_back()\n'
    '  user: "strafe left"              -> robot_strafe_left()\n'
    '  user: "turn right"               -> robot_turn_right()\n'
    '  user: "walk and turn left"       -> robot_walk_turn(vx=0.45, vyaw=0.55, duration_s=0.8)\n'
    '  user: "dance"                    -> robot_dance()\n'
    '  user: "greet" / "say hi"         -> robot_greet()\n'
    '  user: "jump"                     -> robot_jump()\n'
    '  user: "pounce"                   -> robot_pounce()\n'
    '  user: "explore for five seconds" -> robot_explore_room(duration_s=5)\n'
    '  user: "stop" / "halt"            -> robot_stop()\n'
    '  user: "lie down" / "sit"         -> robot_sit_down()\n'
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
    _empty_tool("robot_dance", "Run a Go2 dance action."),
    _empty_tool("robot_jump", "Run a Go2 jump action if this firmware exposes one."),
    _empty_tool("robot_pounce", "Run a Go2 pounce action if this firmware exposes one."),
    _empty_tool("robot_stretch", "Run a Go2 stretch action."),
    _empty_tool("robot_wiggle", "Run a Go2 wiggle-hips action if exposed."),
    {
        "type": "function",
        "function": {
            "name": "robot_walk_turn",
            "description": "Walk and turn at the same time for a short duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vx": {"type": "number", "description": "Forward velocity. Positive forward."},
                    "vyaw": {"type": "number", "description": "Yaw rate. Positive turns left/CCW."},
                    "duration_s": {"type": "number", "description": "Duration in seconds."},
                },
                "required": ["vx", "vyaw"],
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
                    "vx": {"type": "number", "description": "Forward velocity, m/s. Positive = forward."},
                    "vy": {"type": "number", "description": "Lateral velocity, m/s. Positive = left."},
                    "vyaw": {"type": "number", "description": "Yaw rate, rad/s. Positive = CCW."},
                    "duration_s": {"type": "number", "description": "How long to apply the velocity, seconds."},
                },
                "required": ["vx"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "robot_explore_room",
            "description": "Explore with short telemetry-gated forward/turn steps. Requires ENABLE_EXPLORATION=1.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_s": {"type": "number", "description": "Exploration duration in seconds, max 8."},
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
        self._tools: dict[str, Callable[..., Awaitable[None]]] = {
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
            "robot_walk_turn": self._tool_walk_turn,
            "robot_greet": self._tool_greet,
            "robot_dance": self._tool_dance,
            "robot_jump": self._tool_jump,
            "robot_pounce": self._tool_pounce,
            "robot_stretch": self._tool_stretch,
            "robot_wiggle": self._tool_wiggle,
            "robot_explore_room": self._tool_explore_room,
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

    async def _tool_walk_turn(
        self,
        vx: float = 0.35,
        vyaw: float = 0.45,
        duration_s: float = DEFAULT_MOVE_DURATION_S,
        **_: Any,
    ) -> None:
        await self._tool_move(vx=vx, vy=0.0, vyaw=vyaw, duration_s=duration_s)

    async def _tool_greet(self, **_: Any) -> None:
        await self._client.advanced_action("greet")

    async def _tool_dance(self, **_: Any) -> None:
        await self._client.advanced_action("dance")

    async def _tool_jump(self, **_: Any) -> None:
        await self._client.advanced_action("jump")

    async def _tool_pounce(self, **_: Any) -> None:
        await self._client.advanced_action("pounce")

    async def _tool_stretch(self, **_: Any) -> None:
        await self._client.advanced_action("stretch")

    async def _tool_wiggle(self, **_: Any) -> None:
        await self._client.advanced_action("wiggle")

    async def _tool_explore_room(self, duration_s: float = 3.0, **_: Any) -> None:
        value = float(duration_s)
        if not math.isfinite(value):
            raise ValueError("duration_s must be finite")
        await self._client.explore_room(value)

    async def _tool_move(
        self,
        vx: float = 0.0,
        vy: float = 0.0,
        vyaw: float = 0.0,
        duration_s: float = DEFAULT_MOVE_DURATION_S,
        **_: Any,
    ) -> None:
        values = (float(vx), float(vy), float(vyaw), float(duration_s))
        if not all(math.isfinite(v) for v in values):
            raise ValueError("move arguments must be finite numbers")
        safe_duration = min(max(0.0, values[3]), MAX_MOVE_DURATION_S)
        await self._client.move(values[0], values[1], values[2], safe_duration)

    async def handle(self, user_text: str) -> str:
        """Ask the model what to do, then run the first tool call."""
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]

        try:
            response = await asyncio.to_thread(
                ollama.chat,
                model=self._model,
                messages=messages,
                tools=_TOOL_SCHEMAS,
            )
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
            await fn(**args)
        except TypeError as exc:
            await self._client.stop()
            return f"bad args for {name}: {exc} -> stop"
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            await self._client.stop()
            return f"tool {name} failed: {exc} -> stop"

        return f"called {name}({_format_args(args)})"

    async def repl(self) -> None:
        """Read prompts in a worker thread and dispatch them."""
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
    """Pull tool calls out of an Ollama chat response, robust to dict/object form."""
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
    """Read a field from either a dict or a pydantic-style object."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _format_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items())
