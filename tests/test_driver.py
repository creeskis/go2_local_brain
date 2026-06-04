"""Driver-layer tests that don't need hardware.

We fake the upstream pub/sub + channel so we can assert the exact wire
envelope and the closed-channel guard.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from typing import Any

from go2_local_brain.driver.webrtc_client import (
    Go2Config,
    Go2WebRTCClient,
    _extract_motion_mode_name,
)


class _FakeChannel:
    def __init__(self, ready: bool = True) -> None:
        self.readyState = "open" if ready else "closed"
        self.sent: list[str] = []

    def send(self, message: str) -> None:
        self.sent.append(message)


class _FakePubSub:
    def __init__(self, ready: bool = True) -> None:
        self.channel = _FakeChannel(ready=ready)
        self.published: list[tuple[str, dict, str | None]] = []
        self.requests: list[tuple[str, dict]] = []

    def publish_without_callback(self, topic: str, data: Any = None, msg_type: Any = None) -> None:
        if self.channel.readyState != "open":
            return
        self.published.append((topic, data, msg_type))

    async def publish_request_new(self, topic: str, options: dict) -> None:
        self.requests.append((topic, options))


def _make_client_with_fake(pubsub: _FakePubSub, cfg: Go2Config | None = None) -> Go2WebRTCClient:
    """Build a client and wire it to a fake pub/sub without running connect()."""
    client = Go2WebRTCClient(cfg or Go2Config(ip="127.0.0.1"))
    client._pubsub = pubsub
    client._sport_topic = "rt/api/sport/request"
    client._sport_cmd = {
        "Move": 1008,
        "StopMove": 1003,
        "StandUp": 1004,
        "BalanceStand": 1002,
        "Hello": 1016,
        "Dance1": 1022,
    }
    return client


class PublishMoveTests(unittest.TestCase):
    def test_open_channel_publishes_expected_envelope(self) -> None:
        pubsub = _FakePubSub(ready=True)
        client = _make_client_with_fake(pubsub)

        client._publish_move(0.1, -0.05, 0.2)

        self.assertEqual(len(pubsub.published), 1)
        topic, payload, _msg_type = pubsub.published[0]
        self.assertEqual(topic, "rt/api/sport/request")
        self.assertEqual(payload["header"]["identity"]["api_id"], 1008)
        parameter = json.loads(payload["parameter"])
        self.assertAlmostEqual(parameter["x"], 0.1)
        self.assertAlmostEqual(parameter["y"], -0.05)
        self.assertAlmostEqual(parameter["z"], 0.2)

    def test_closed_channel_raises(self) -> None:
        pubsub = _FakePubSub(ready=False)
        client = _make_client_with_fake(pubsub)
        with self.assertRaises(RuntimeError):
            client._publish_move(0.0, 0.0, 0.0)
        self.assertEqual(pubsub.published, [])

    def test_not_connected_raises(self) -> None:
        client = Go2WebRTCClient(Go2Config(ip="127.0.0.1"))
        with self.assertRaises(RuntimeError):
            client._publish_move(0.0, 0.0, 0.0)


class SportStateTests(unittest.TestCase):
    def test_callback_caches_and_summarizes(self) -> None:
        client = _make_client_with_fake(_FakePubSub())
        client._on_sport_state({"data": {"mode": 1, "gait_type": 2}})
        self.assertEqual(client._sport_state.get("mode"), 1)
        self.assertEqual(client._sport_state_summary, (1, 2))

    def test_callback_does_not_collapse_zero(self) -> None:
        client = _make_client_with_fake(_FakePubSub())
        client._on_sport_state({"data": {"mode": 0, "gait_type": 0}})
        self.assertEqual(client._sport_state_summary, (0, 0))

    def test_callback_ignores_garbage(self) -> None:
        client = _make_client_with_fake(_FakePubSub())
        client._on_sport_state({"data": "not-a-dict"})
        self.assertEqual(client._sport_state, {})


class MoveClampingTests(unittest.TestCase):
    def test_move_clamps_velocity(self) -> None:
        pubsub = _FakePubSub(ready=True)
        client = _make_client_with_fake(pubsub)
        asyncio.run(client.move(vx=10.0, duration_s=0.05))
        self.assertTrue(pubsub.published)
        for _topic, payload, _msg_type in pubsub.published:
            param = json.loads(payload["parameter"])
            self.assertLessEqual(param["x"], 0.75 + 1e-9)


class AdvancedActionTests(unittest.TestCase):
    def test_dance_uses_first_available_candidate(self) -> None:
        pubsub = _FakePubSub()
        client = _make_client_with_fake(pubsub)
        asyncio.run(client.advanced_action("dance"))
        self.assertEqual(pubsub.requests[-1], ("rt/api/sport/request", {"api_id": 1022}))

    def test_unknown_advanced_action_raises(self) -> None:
        client = _make_client_with_fake(_FakePubSub())
        with self.assertRaises(RuntimeError):
            asyncio.run(client.advanced_action("teleport"))


class ExplorationGuardTests(unittest.TestCase):
    def test_exploration_disabled_raises(self) -> None:
        client = _make_client_with_fake(_FakePubSub())
        with self.assertRaises(RuntimeError):
            asyncio.run(client.explore_room(0.1))

    def test_all_zero_range_obstacles_are_unavailable(self) -> None:
        cfg = Go2Config(ip="127.0.0.1", enable_exploration=True)
        client = _make_client_with_fake(_FakePubSub(), cfg)
        client._sport_state = {"range_obstacle": [0, 0, 0, 0]}
        client._sport_state_ts = time.monotonic()
        self.assertIsNone(client._valid_range_obstacles())

    def test_nonzero_range_obstacles_are_valid(self) -> None:
        cfg = Go2Config(ip="127.0.0.1", enable_exploration=True)
        client = _make_client_with_fake(_FakePubSub(), cfg)
        client._sport_state = {"range_obstacle": [1.0, 0.8, 0.6, 0.7]}
        client._sport_state_ts = time.monotonic()
        self.assertEqual(client._valid_range_obstacles(), [1.0, 0.8, 0.6, 0.7])


class StopResilienceTests(unittest.TestCase):
    """stop() is the universal panic button and must never raise."""

    def test_stop_swallows_publish_failure(self) -> None:
        pubsub = _FakePubSub(ready=False)

        async def boom(*_a, **_kw):
            raise RuntimeError("simulated channel-closed during stop")

        pubsub.publish_request_new = boom  # type: ignore[assignment]
        client = _make_client_with_fake(pubsub)
        asyncio.run(client.stop())


class MotionModeNameExtractorTests(unittest.TestCase):
    def test_dict_data(self) -> None:
        self.assertEqual(_extract_motion_mode_name({"data": {"name": "normal"}}), "normal")

    def test_json_string_data(self) -> None:
        self.assertEqual(_extract_motion_mode_name({"data": '{"name": "mcf"}'}), "mcf")

    def test_object_response(self) -> None:
        class FakeResp:
            data = {"name": "ai"}

        self.assertEqual(_extract_motion_mode_name(FakeResp()), "ai")

    def test_missing_name(self) -> None:
        self.assertIsNone(_extract_motion_mode_name({"data": {}}))

    def test_none(self) -> None:
        self.assertIsNone(_extract_motion_mode_name(None))


if __name__ == "__main__":
    unittest.main()
