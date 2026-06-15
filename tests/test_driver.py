"""Driver-layer tests that don't need hardware."""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from typing import Any
from unittest.mock import AsyncMock

from go2_local_brain.driver.webrtc_client import (
    Go2Config,
    Go2WebRTCClient,
    _build_unitree_connection,
    _extract_motion_mode_name,
    _friendly_connect_error,
    _method_name,
    _merge_sport_cmds,
    _resolve_webrtc_method,
)
from go2_local_brain.safety.limits import MAX_VX


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
        "Handstand": 1301,
    }
    client._sport_cmd_mcf = {
        "HandStand": 2044,
        "BackStand": 2050,
        "FrontFlip": 1030,
        "BackFlip": 2043,
        "LeftFlip": 2041,
    }
    # RightFlip is base-only on the real SDK; mirror that here.
    client._sport_cmd["RightFlip"] = 1043
    client._sport_cmd["FrontFlip"] = 1030
    return client


class PublishMoveTests(unittest.TestCase):
    def test_open_channel_publishes_expected_envelope(self) -> None:
        pubsub = _FakePubSub(ready=True)
        client = _make_client_with_fake(pubsub)
        client._publish_move(0.1, -0.05, 0.2)
        topic, payload, _msg_type = pubsub.published[0]
        self.assertEqual(topic, "rt/api/sport/request")
        self.assertEqual(payload["header"]["identity"]["api_id"], 1008)
        parameter = json.loads(payload["parameter"])
        self.assertAlmostEqual(parameter["x"], 0.1)
        self.assertAlmostEqual(parameter["y"], -0.05)
        self.assertAlmostEqual(parameter["z"], 0.2)

    def test_closed_channel_raises(self) -> None:
        client = _make_client_with_fake(_FakePubSub(ready=False))
        with self.assertRaises(RuntimeError):
            client._publish_move(0.0, 0.0, 0.0)

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


class MovementFeatureTests(unittest.TestCase):
    def test_move_clamps_velocity(self) -> None:
        pubsub = _FakePubSub(ready=True)
        client = _make_client_with_fake(pubsub)
        asyncio.run(client.move(vx=10.0, duration_s=0.05))
        self.assertTrue(pubsub.published)
        for _topic, payload, _msg_type in pubsub.published:
            param = json.loads(payload["parameter"])
            self.assertLessEqual(param["x"], MAX_VX + 1e-9)

    def test_turn_180_calls_move_with_full_turn_duration(self) -> None:
        client = _make_client_with_fake(_FakePubSub())
        client.move = AsyncMock()  # type: ignore[method-assign]
        asyncio.run(client.turn_180("right"))
        args = client.move.call_args.args
        self.assertEqual(args[0], 0.0)
        self.assertLess(args[2], 0.0)
        self.assertGreater(args[3], 3.0)

    def test_sequence_runs_known_steps_then_stops(self) -> None:
        client = _make_client_with_fake(_FakePubSub())
        client.move = AsyncMock()  # type: ignore[method-assign]
        client.stop = AsyncMock()  # type: ignore[method-assign]
        asyncio.run(client.sequence([{"cmd": "forward"}, {"cmd": "turn_180_left"}]))
        self.assertGreaterEqual(client.move.await_count, 1)
        client.stop.assert_awaited()

    def test_sequence_accepts_model_generated_aliases(self) -> None:
        client = _make_client_with_fake(_FakePubSub())
        client.move = AsyncMock()  # type: ignore[method-assign]
        client.stop = AsyncMock()  # type: ignore[method-assign]
        asyncio.run(client.sequence([{"cmd": "robotstep_forward"}, {"cmd": "robot_turn_right_90"}]))
        self.assertEqual(client.move.await_count, 2)
        client.stop.assert_awaited()


class SportCommandMergeTests(unittest.TestCase):
    def test_mcf_commands_are_added_without_overriding_base_ids(self) -> None:
        merged = _merge_sport_cmds({"Move": 1008}, {"Move": 9999, "BackStand": 2050})
        self.assertEqual(merged["Move"], 1008)
        self.assertEqual(merged["BackStand"], 2050)


class AdvancedActionTests(unittest.TestCase):
    def test_dance_uses_first_available_candidate(self) -> None:
        pubsub = _FakePubSub()
        client = _make_client_with_fake(pubsub)
        asyncio.run(client.advanced_action("dance"))
        self.assertEqual(pubsub.requests[-1], ("rt/api/sport/request", {"api_id": 1022}))

    def test_backstand_uses_mcf_candidate(self) -> None:
        pubsub = _FakePubSub()
        client = _make_client_with_fake(pubsub)
        asyncio.run(client.advanced_action("backstand"))
        self.assertEqual(pubsub.requests[-1], ("rt/api/sport/request", {"api_id": 2050, "parameter": {"data": True}}))

    def test_directional_flips_resolve(self) -> None:
        # front/back/left flips resolve via the MCF table; right flip is
        # base-only and must still resolve.
        cases = {
            "front_flip": 1030,
            "back_flip": 2043,
            "left_flip": 2041,
            "right_flip": 1043,
        }
        for action, api_id in cases.items():
            pubsub = _FakePubSub()
            client = _make_client_with_fake(pubsub)
            asyncio.run(client.advanced_action(action))
            self.assertEqual(
                pubsub.requests[-1],
                ("rt/api/sport/request", {"api_id": api_id, "parameter": {"data": True}}),
                f"{action} resolved wrong",
            )

    def test_exact_sport_command_uses_mcf_candidate(self) -> None:
        pubsub = _FakePubSub()
        client = _make_client_with_fake(pubsub)
        asyncio.run(client.sport_command("BackStand", {"data": True}))
        self.assertEqual(pubsub.requests[-1], ("rt/api/sport/request", {"api_id": 2050, "parameter": {"data": True}}))

    def test_available_sport_commands_includes_base_and_mcf(self) -> None:
        client = _make_client_with_fake(_FakePubSub())
        commands = client.available_sport_commands()
        self.assertIn("Move", commands)
        self.assertIn("BackStand", commands)

    def test_handstand_prefers_mcf_toggle_candidate(self) -> None:
        pubsub = _FakePubSub()
        client = _make_client_with_fake(pubsub)
        asyncio.run(client.advanced_action("handstand"))
        self.assertEqual(pubsub.requests[-1], ("rt/api/sport/request", {"api_id": 2044, "parameter": {"data": True}}))

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

    def test_blind_exploration_runs_without_ranges(self) -> None:
        cfg = Go2Config(ip="127.0.0.1", enable_exploration=True, exploration_mode="blind")
        client = _make_client_with_fake(_FakePubSub(), cfg)
        client.move = AsyncMock()  # type: ignore[method-assign]
        client.stop = AsyncMock()  # type: ignore[method-assign]
        asyncio.run(client.explore_room(0.05))
        client.move.assert_awaited()
        client.stop.assert_awaited()

    def test_telemetry_report_explains_zero_ranges(self) -> None:
        client = _make_client_with_fake(_FakePubSub())
        client._sport_state = {"range_obstacle": [0, 0, 0, 0], "mode": 0}
        client._sport_state_ts = time.monotonic()
        report = client.telemetry_report()
        self.assertIn("range_status=unavailable", report)


class StopResilienceTests(unittest.TestCase):
    def test_stop_swallows_publish_failure(self) -> None:
        pubsub = _FakePubSub(ready=False)

        async def boom(*_a, **_kw):
            raise RuntimeError("simulated channel-closed during stop")

        pubsub.publish_request_new = boom  # type: ignore[assignment]
        client = _make_client_with_fake(pubsub)
        asyncio.run(client.stop())


class WebRTCConfigTests(unittest.TestCase):
    class FakeMethod:
        LocalSTA = object()
        LocalAP = object()
        Remote = object()

    def test_resolve_webrtc_method_accepts_aliases(self) -> None:
        self.assertIs(_resolve_webrtc_method(self.FakeMethod, "sta"), self.FakeMethod.LocalSTA)
        self.assertIs(_resolve_webrtc_method(self.FakeMethod, "LocalAP"), self.FakeMethod.LocalAP)
        self.assertIs(_resolve_webrtc_method(self.FakeMethod, "remote"), self.FakeMethod.Remote)

    def test_connection_kwargs_local_ap_does_not_force_ip(self) -> None:
        client = Go2WebRTCClient(Go2Config(ip="192.168.123.121", webrtc_method="LocalAP"))

        class Method:
            name = "LocalAP"

        kwargs = client._connection_kwargs(Method())
        self.assertNotIn("ip", kwargs)

    def test_friendly_no_sdp_error_explains_network_recovery(self) -> None:
        class NoSdpAnswerError(Exception):
            pass

        class Method:
            name = "LocalSTA"

        msg = _friendly_connect_error(NoSdpAnswerError("Robot signaling returned no SDP answer"), Go2Config(ip="192.168.123.121"), Method())
        self.assertIn("returned no SDP answer", msg)
        self.assertIn("GO2_WEBRTC_METHOD=LocalSTA", msg)

    def test_method_name_handles_plain_object(self) -> None:
        class Method:
            name = "LocalSTA"

        self.assertEqual(_method_name(Method()), "LocalSTA")

    def test_build_connection_prefers_positional_method(self) -> None:
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def factory(*args: Any, **kwargs: Any) -> object:
            calls.append((args, kwargs))
            return {"ok": True}

        method = object()
        conn = _build_unitree_connection(factory, method, {"connectionMethod": method, "ip": "192.168.123.121"})
        self.assertEqual(conn, {"ok": True})
        self.assertEqual(calls[0][0], (method,))
        self.assertEqual(calls[0][1]["ip"], "192.168.123.121")

    def test_build_connection_falls_back_to_legacy_aes_key_name(self) -> None:
        calls: list[dict[str, Any]] = []

        def factory(*_args: Any, **kwargs: Any) -> object:
            calls.append(kwargs)
            if "aes_128_key" in kwargs:
                raise TypeError("unexpected keyword")
            return {"ok": True}

        method = object()
        conn = _build_unitree_connection(factory, method, {"connectionMethod": method, "ip": "192.168.123.121"}, "key")
        self.assertEqual(conn, {"ok": True})
        self.assertIn("aesKey", calls[-1])


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
