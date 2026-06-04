"""Unit tests for the brain layer that don't need hardware.

Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock

from go2_local_brain.brain.local_llm import LocalRobotBrain, _extract_tool_calls
from go2_local_brain.safety.limits import MAX_MOVE_DURATION_S


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class ExtractToolCallsTests(unittest.TestCase):
    """`_extract_tool_calls` must handle the three shapes Ollama can return."""

    def test_dict_arguments(self) -> None:
        # Most common: arguments come as a dict already.
        response = {
            "message": {
                "tool_calls": [
                    {"function": {"name": "robot_move", "arguments": {"vx": 0.2}}}
                ]
            }
        }
        self.assertEqual(
            _extract_tool_calls(response),
            [{"name": "robot_move", "arguments": {"vx": 0.2}}],
        )

    def test_json_string_arguments(self) -> None:
        # Some models hand back arguments as a JSON-encoded string.
        response = {
            "message": {
                "tool_calls": [
                    {"function": {"name": "robot_move", "arguments": '{"vx": 0.1}'}}
                ]
            }
        }
        self.assertEqual(
            _extract_tool_calls(response),
            [{"name": "robot_move", "arguments": {"vx": 0.1}}],
        )

    def test_object_response(self) -> None:
        # ollama.chat() can return pydantic-style objects, not dicts.
        class FakeFn:
            name = "robot_stop"
            arguments = {}

        class FakeCall:
            function = FakeFn()

        class FakeMsg:
            tool_calls = [FakeCall()]

        class FakeResp:
            message = FakeMsg()

        self.assertEqual(
            _extract_tool_calls(FakeResp()),
            [{"name": "robot_stop", "arguments": {}}],
        )

    def test_no_message(self) -> None:
        self.assertEqual(_extract_tool_calls({}), [])

    def test_no_tool_calls(self) -> None:
        self.assertEqual(_extract_tool_calls({"message": {"content": "hi"}}), [])

    def test_garbage_json_string(self) -> None:
        # Malformed JSON in arguments should not crash; defaults to {}.
        response = {
            "message": {
                "tool_calls": [
                    {"function": {"name": "robot_stop", "arguments": "not-json"}}
                ]
            }
        }
        self.assertEqual(
            _extract_tool_calls(response),
            [{"name": "robot_stop", "arguments": {}}],
        )


class ToolMoveValidationTests(unittest.TestCase):
    """`_tool_move` must reject non-finite args and cap duration."""

    def setUp(self) -> None:
        # AsyncMock stands in for Go2WebRTCClient.move().
        self.client = unittest.mock.MagicMock()
        self.client.move = AsyncMock()
        self.client.stop = AsyncMock()
        self.brain = LocalRobotBrain(self.client, model="qwen3:1.7b")

    def test_caps_overlong_duration(self) -> None:
        asyncio.run(self.brain._tool_move(vx=0.1, duration_s=999.0))
        self.client.move.assert_awaited_once()
        _, kwargs = self.client.move.call_args
        # client.move() takes positional args; check those instead.
        args = self.client.move.call_args.args
        self.assertEqual(args[0], 0.1)
        self.assertEqual(args[3], MAX_MOVE_DURATION_S)

    def test_floors_negative_duration(self) -> None:
        asyncio.run(self.brain._tool_move(vx=0.1, duration_s=-3.0))
        args = self.client.move.call_args.args
        self.assertEqual(args[3], 0.0)

    def test_rejects_nan(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(self.brain._tool_move(vx=float("nan")))
        self.client.move.assert_not_awaited()

    def test_rejects_inf(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(self.brain._tool_move(vx=0.1, vyaw=float("inf")))
        self.client.move.assert_not_awaited()


class HandleDispatchTests(unittest.TestCase):
    """`handle()` should stop on unknown / missing tool calls."""

    def _make_brain(self, fake_response: Any) -> tuple[LocalRobotBrain, Any]:
        client = unittest.mock.MagicMock()
        client.move = AsyncMock()
        client.stop = AsyncMock()
        client.stand_up = AsyncMock()
        client.sit_down = AsyncMock()
        brain = LocalRobotBrain(client, model="qwen3:1.7b")

        # Patch ollama.chat in the brain module so we don't hit a server.
        import go2_local_brain.brain.local_llm as mod
        self._orig_chat = mod.ollama.chat
        mod.ollama.chat = unittest.mock.MagicMock(return_value=fake_response)
        return brain, client

    def tearDown(self) -> None:
        import go2_local_brain.brain.local_llm as mod
        if hasattr(self, "_orig_chat"):
            mod.ollama.chat = self._orig_chat

    def test_unknown_tool_stops(self) -> None:
        brain, client = self._make_brain(
            {"message": {"tool_calls": [{"function": {"name": "robot_dance", "arguments": {}}}]}}
        )
        result = asyncio.run(brain.handle("dance"))
        client.stop.assert_awaited()
        self.assertIn("unknown tool", result)

    def test_no_tool_call_stops(self) -> None:
        brain, client = self._make_brain({"message": {"content": "ok"}})
        result = asyncio.run(brain.handle("hello"))
        client.stop.assert_awaited()
        self.assertIn("no tool call", result)

    def test_stand_up_dispatch(self) -> None:
        brain, client = self._make_brain(
            {"message": {"tool_calls": [{"function": {"name": "robot_stand_up", "arguments": {}}}]}}
        )
        result = asyncio.run(brain.handle("up"))
        client.stand_up.assert_awaited_once()
        self.assertIn("robot_stand_up", result)


if __name__ == "__main__":
    unittest.main()
