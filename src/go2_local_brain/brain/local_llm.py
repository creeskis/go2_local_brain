"""Local LLM brain: turn typed prompts into a single robot tool call.

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
    "On every user instruction, choose exactly ONE of the available tools "
    "and call it with conservative arguments. Velocities are in m/s and rad/s. "
    "Positive vx is forward, positive vy is left, positive vyaw is "
    "counter-clockwise. Keep |vx| <= 0.3, |vy| <= 0.15, |vyaw| <= 0.4, "
    "duration_s <= 1.0. If the user wants the robot to halt, or you are "
    "unsure, call robot_stop. Do not invent new tools.\n"
    "\n"
    "Examples:\n"
    '  user: "stand up"            -> robot_stand_up()\n'
    '  user: "go forward a step"   -> robot_move(vx=0.2, duration_s=0.6)\n'
    '  user: "turn left slowly"    -> robot_move(vx=0, vyaw=0.3, duration_s=0.5)\n'
    '  user: "stop" / "halt"       -> robot_stop()\n'
    '  user: "lie down" / "sit"    -> robot_sit_down()\n'
)


# --------------------------------------------------------------------- schemas
# Ollama accepts OpenAI-style tool definitions. Keep arguments minimal so a
# small local model can fill them reliably.
_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "robot_stand_up",
            "description": "Make the robot stand up from a sitting/lying pose.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "robot_sit_down",
            "description": "Make the robot sit / lie down.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "robot_stop",
            "description": "Immediately stop all motion (zero velocity).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "robot_move",
            "description": (
                "Drive the robot at a constant velocity for a short duration. "
                "Defaults are safe; only fill what you need."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vx": {
                        "type": "number",
                        "description": "Forward velocity, m/s. Positive = forward.",
                    },
                    "vy": {
                        "type": "number",
                        "description": "Lateral velocity, m/s. Positive = left.",
                    },
                    "vyaw": {
                        "type": "number",
                        "description": "Yaw rate, rad/s. Positive = CCW.",
                    },
                    "duration_s": {
                        "type": "number",
                        "description": "How long to apply the velocity, in seconds.",
                    },
                },
                "required": ["vx"],
            },
        },
    },
]


class LocalRobotBrain:
    """Routes natural-language prompts through Ollama into robot tool calls."""

    def __init__(self, client: Go2WebRTCClient, model: str) -> None:
        self._client = client
        self._model = model
        # Maps tool names to async callables. The brain is the only place
        # that "knows" how each tool maps onto the driver.
        self._tools: dict[str, Callable[..., Awaitable[None]]] = {
            "robot_stand_up": self._tool_stand_up,
            "robot_sit_down": self._tool_sit_down,
            "robot_stop": self._tool_stop,
            "robot_move": self._tool_move,
        }

    # ------------------------------------------------------------- tool bodies

    async def _tool_stand_up(self, **_: Any) -> None:
        await self._client.stand_up()

    async def _tool_sit_down(self, **_: Any) -> None:
        await self._client.sit_down()

    async def _tool_stop(self, **_: Any) -> None:
        await self._client.stop()

    async def _tool_move(
        self,
        vx: float = 0.0,
        vy: float = 0.0,
        vyaw: float = 0.0,
        duration_s: float = DEFAULT_MOVE_DURATION_S,
        **_: Any,
    ) -> None:
        # Defend against NaN/inf and runaway durations *before* they reach
        # the driver. The driver clamps velocity magnitudes but trusts the
        # duration; a hallucinated 600s here would otherwise pin the robot.
        values = (float(vx), float(vy), float(vyaw), float(duration_s))
        if not all(math.isfinite(v) for v in values):
            raise ValueError("move arguments must be finite numbers")
        safe_duration = min(max(0.0, values[3]), MAX_MOVE_DURATION_S)
        await self._client.move(values[0], values[1], values[2], safe_duration)

    # --------------------------------------------------------------- handle one

    async def handle(self, user_text: str) -> str:
        """Ask the model what to do, then run the first tool call. Returns a log line."""
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]

        try:
            # ollama.chat is blocking - push it off the event loop.
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
            # Bad argument shape from the model; stop and report.
            await self._client.stop()
            return f"bad args for {name}: {exc} -> stop"
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            await self._client.stop()
            return f"tool {name} failed: {exc} -> stop"

        return f"called {name}({_format_args(args)})"

    # --------------------------------------------------------------------- repl

    async def repl(self) -> None:
        """Read prompts in a worker thread and dispatch them.

        Blank line is ignored; ``quit`` / ``exit`` ends the REPL. We never
        block the asyncio loop because input() runs via asyncio.to_thread.
        """
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


# ----------------------------------------------------------------- helpers

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
        # Some models return arguments as a JSON string; normalize to dict.
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
