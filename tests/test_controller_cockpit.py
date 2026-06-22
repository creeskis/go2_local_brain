"""Tests for the video-only controller cockpit mappings."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from go2_local_brain.controller_cockpit import ControllerCockpit, _INDEX_HTML


class ControllerActionTests(unittest.TestCase):
    def test_posture_actions_use_driver_helpers(self) -> None:
        cockpit = ControllerCockpit("127.0.0.1", 0)
        client = AsyncMock()
        cockpit._client = client
        asyncio.run(cockpit._run_action("stand_up"))
        client.stop.assert_awaited()
        client.stand_up.assert_awaited_once()
        client.advanced_action.assert_not_awaited()

        client.reset_mock()
        asyncio.run(cockpit._run_action("sit_down"))
        client.sit_down.assert_awaited_once()

    def test_flip_jump_pounce_and_backstand_use_advanced_actions(self) -> None:
        for action in (
            "right_flip",
            "left_flip",
            "front_flip",
            "back_flip",
            "jump",
            "pounce",
            "backstand",
        ):
            cockpit = ControllerCockpit("127.0.0.1", 0)
            client = AsyncMock()
            cockpit._client = client
            asyncio.run(cockpit._run_action(action))
            client.advanced_action.assert_awaited_once_with(action)


class ControllerHtmlTests(unittest.TestCase):
    def test_requested_gamepad_indices_are_present(self) -> None:
        expected = {
            'edge(pad,5,"rb",()=>action("right_flip"))',
            'edge(pad,4,"lb",()=>action("left_flip"))',
            'edge(pad,3,"y",()=>action("stand_up"))',
            'edge(pad,1,"b",()=>action("sit_down"))',
            'edge(pad,0,"a",()=>action("jump"))',
            'edge(pad,2,"x",()=>action("pounce"))',
            'edge(pad,12,"dup",()=>toggleGait("jump"))',
            'edge(pad,13,"ddown",()=>toggleGait("bound"))',
            'edge(pad,14,"dleft",()=>updateSpeed(-1))',
            'edge(pad,15,"dright",()=>updateSpeed(1))',
        }
        for mapping in expected:
            self.assertIn(mapping, _INDEX_HTML)

    def test_triggers_use_filtered_analog_values(self) -> None:
        self.assertIn("filteredRT=smooth(filteredRT,buttonValue(pad,7),dt,.04)", _INDEX_HTML)
        self.assertIn("filteredLT=smooth(filteredLT,buttonValue(pad,6),dt,.05)", _INDEX_HTML)
        self.assertIn("smoothstep", _INDEX_HTML)

    def test_controller_uses_persistent_low_latency_socket(self) -> None:
        self.assertIn("/ws/control", _INDEX_HTML)
        self.assertIn("new WebSocket", _INDEX_HTML)

    def test_dpad_up_down_toggle_gaits(self) -> None:
        self.assertIn('edge(pad,12,"dup",()=>toggleGait("jump"))', _INDEX_HTML)
        self.assertIn('edge(pad,13,"ddown",()=>toggleGait("bound"))', _INDEX_HTML)
        self.assertIn("/api/gait", _INDEX_HTML)


class ControllerGaitTests(unittest.TestCase):
    def test_apply_gait_sends_free_commands(self) -> None:
        cockpit = ControllerCockpit("127.0.0.1", 0)
        client = AsyncMock()
        cockpit._client = client
        asyncio.run(cockpit._apply_gait("jump"))
        client.sport_command.assert_awaited_with("FreeJump", {"data": True})
        self.assertEqual(cockpit._gait, "jump")
        asyncio.run(cockpit._apply_gait("bound"))
        client.sport_command.assert_awaited_with("FreeBound", {"data": True})
        self.assertEqual(cockpit._gait, "bound")
        asyncio.run(cockpit._apply_gait("walk"))
        client.sport_command.assert_awaited_with("FreeWalk", {"data": True})
        self.assertEqual(cockpit._gait, "walk")

    def test_gait_toggle_returns_to_walk_when_repeated(self) -> None:
        cockpit = ControllerCockpit("127.0.0.1", 0)
        cockpit._client = AsyncMock()

        class _Req:
            def __init__(self, body: dict) -> None:
                self._body = body

            async def json(self) -> dict:
                return self._body

        # First press enters jump; second press of the same toggles back to walk.
        first = asyncio.run(cockpit._gait_toggle(_Req({"gait": "jump"})))
        self.assertEqual(first.status, 200)
        self.assertEqual(cockpit._gait, "jump")
        asyncio.run(cockpit._gait_toggle(_Req({"gait": "jump"})))
        self.assertEqual(cockpit._gait, "walk")

    def test_unknown_gait_rejected(self) -> None:
        cockpit = ControllerCockpit("127.0.0.1", 0)

        class _Req:
            async def json(self) -> dict:
                return {"gait": "moonwalk"}

        response = asyncio.run(cockpit._gait_toggle(_Req()))
        self.assertEqual(response.status, 400)

    def test_one_shot_action_resets_gait_to_walk(self) -> None:
        cockpit = ControllerCockpit("127.0.0.1", 0)
        cockpit._client = AsyncMock()
        cockpit._gait = "bound"
        asyncio.run(cockpit._run_action("jump"))
        self.assertEqual(cockpit._gait, "walk")


if __name__ == "__main__":
    unittest.main()
