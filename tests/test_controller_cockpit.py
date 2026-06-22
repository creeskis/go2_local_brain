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
            'edge(pad,12,"dup",()=>action("front_flip"))',
            'edge(pad,13,"ddown",()=>action("back_flip"))',
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


if __name__ == "__main__":
    unittest.main()
