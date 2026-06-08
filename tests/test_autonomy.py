"""Tests for first-pass autonomy helpers."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from go2_local_brain.autonomy.follow import HumanFollowController, SoundCue
from go2_local_brain.autonomy.local_map import LocalMapState, normalize_radians, raw_pose_from_sport_state
from go2_local_brain.autonomy.map import PatrolMap, Waypoint, list_patrol_maps, load_patrol_map, save_patrol_map
from go2_local_brain.autonomy.navigator import AutonomyNavigator
from go2_local_brain.autonomy.perception import (
    CameraOnlyPerceptionProvider,
    Detection,
    Observation,
    PerceptionProvider,
    YoloPerceptionProvider,
    best_human_detection,
)
from go2_local_brain.autonomy.supervisor import AutonomySupervisor


class FakeClient:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float, float, float]] = []
        self.stops = 0
        self.sport_state: dict[str, object] | None = None

    async def move(self, vx: float, vy: float = 0.0, vyaw: float = 0.0, duration_s: float = 0.0) -> None:
        self.moves.append((vx, vy, vyaw, duration_s))

    async def stop(self) -> None:
        self.stops += 1


class StaticPerception(PerceptionProvider):
    def __init__(self, observation: Observation) -> None:
        self.observation = observation

    async def observe(self) -> Observation:
        return self.observation


def _map() -> PatrolMap:
    return PatrolMap(
        name="test",
        waypoints={
            "home": Waypoint("home", 0.0, 0.0),
            "target": Waypoint("target", 1.0, 0.0),
        },
        patrol_route=["target", "home"],
        no_go_zones=[],
    )


class PatrolMapTests(unittest.TestCase):
    def test_load_patrol_map_validates_route(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "map.json"
            path.write_text(
                json.dumps(
                    {
                        "name": "unit",
                        "waypoints": {"a": {"x": 1, "y": 2}},
                        "patrol_route": ["a"],
                        "no_go_zones": ["stairs"],
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_patrol_map(path)
        self.assertEqual(loaded.name, "unit")
        self.assertEqual(loaded.next_waypoint(0)[1].name, "a")
        self.assertEqual(loaded.no_go_zones, ["stairs"])

    def test_load_patrol_map_rejects_missing_waypoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "map.json"
            path.write_text(
                json.dumps({"waypoints": {"a": {"x": 1, "y": 2}}, "patrol_route": ["missing"]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_patrol_map(path)

    def test_save_and_list_patrol_map(self) -> None:
        patrol_map = _map()
        with tempfile.TemporaryDirectory() as td:
            path = save_patrol_map(patrol_map, td)
            self.assertTrue(path.exists())
            maps = list_patrol_maps(td)
        self.assertEqual(maps[0]["name"], "test")
        self.assertTrue(maps[0]["ready"])

    def test_save_and_list_draft_map(self) -> None:
        patrol_map = PatrolMap(
            name="draft",
            waypoints={"home": Waypoint("home", 0.0, 0.0)},
            patrol_route=[],
            no_go_zones=[],
        )
        with tempfile.TemporaryDirectory() as td:
            save_patrol_map(patrol_map, td)
            maps = list_patrol_maps(td)
        self.assertEqual(maps[0]["name"], "draft")
        self.assertFalse(maps[0]["ready"])


class LocalMapStateTests(unittest.TestCase):
    def test_raw_pose_from_sport_state_extracts_position_and_yaw(self) -> None:
        pose = raw_pose_from_sport_state({"position": [1.5, -2.0, 0.1], "imu_state": {"rpy": [0.0, 0.0, 1.25]}})
        self.assertIsNotNone(pose)
        assert pose is not None
        self.assertAlmostEqual(pose.x, 1.5)
        self.assertAlmostEqual(pose.y, -2.0)
        self.assertAlmostEqual(pose.yaw, 1.25)

    def test_local_map_locks_origin_and_rotates_into_start_frame(self) -> None:
        state = LocalMapState()
        state.update_from_sport_state({"position": [10.0, 20.0, 0.0], "imu_state": {"rpy": [0.0, 0.0, 1.57079632679]}})
        pose = state.update_from_sport_state({"position": [10.0, 21.0, 0.0], "imu_state": {"rpy": [0.0, 0.0, 1.57079632679]}})
        self.assertIsNotNone(pose)
        assert pose is not None
        self.assertAlmostEqual(pose.x, 1.0, places=3)
        self.assertAlmostEqual(pose.y, 0.0, places=3)
        self.assertTrue(state.valid)
        self.assertEqual(state.to_dict()["source"], "sport_state")

    def test_normalize_radians_uses_shortest_angle(self) -> None:
        self.assertAlmostEqual(normalize_radians(3.5), -2.7831853071795862)


class AutonomySupervisorTests(unittest.TestCase):
    def test_step_once_patrols_next_waypoint(self) -> None:
        client = FakeClient()
        perception = StaticPerception(Observation(timestamp=0.0, frame_available=True))
        supervisor = AutonomySupervisor(_map(), AutonomyNavigator(client), perception, tick_s=0.01)
        supervisor._state = "arming"
        asyncio.run(supervisor.step_once())
        status = supervisor.status()
        self.assertEqual(status.state, "patrolling")
        self.assertTrue(client.moves)
        self.assertIn("driving toward target", status.last_action)

    def test_detection_triggers_investigation_scan(self) -> None:
        client = FakeClient()
        perception = StaticPerception(
            Observation(timestamp=0.0, frame_available=True, detections=[Detection("person", 0.80)])
        )
        supervisor = AutonomySupervisor(_map(), AutonomyNavigator(client), perception, tick_s=0.01)
        supervisor._state = "patrolling"
        asyncio.run(supervisor.step_once())
        status = supervisor.status()
        self.assertEqual(status.state, "patrolling")
        self.assertIn("investigate person", status.last_action)
        self.assertGreaterEqual(len(client.moves), 1)

    def test_stop_clears_active_task_and_stops_client(self) -> None:
        client = FakeClient()
        perception = StaticPerception(Observation(timestamp=0.0, frame_available=True))
        supervisor = AutonomySupervisor(_map(), AutonomyNavigator(client), perception, tick_s=0.01)

        async def run() -> None:
            await supervisor.activate()
            await asyncio.sleep(0.02)
            await supervisor.stop()

        asyncio.run(run())
        status = supervisor.status()
        self.assertEqual(status.state, "idle")
        self.assertFalse(status.active)
        self.assertGreater(client.stops, 0)

    def test_navigator_uses_shared_local_map_pose(self) -> None:
        client = FakeClient()
        client.sport_state = {"position": [5.0, 5.0, 0.0], "imu_state": {"rpy": [0.0, 0.0, 0.0]}}
        local_map = LocalMapState()
        navigator = AutonomyNavigator(client, local_map)
        asyncio.run(navigator.move_toward(Waypoint("target", 1.0, 0.0)))
        client.sport_state = {"position": [6.0, 5.0, 0.0], "imu_state": {"rpy": [0.0, 0.0, 0.0]}}
        asyncio.run(navigator.move_toward(Waypoint("target", 1.0, 0.0)))
        self.assertTrue(local_map.valid)
        self.assertAlmostEqual(local_map.pose.x, 1.0, places=3)
        result = asyncio.run(navigator.move_toward(Waypoint("target", 1.0, 0.0)))
        self.assertIn("scan", result)


class PerceptionTests(unittest.TestCase):
    def test_camera_only_provider_is_not_detector_ready(self) -> None:
        provider = CameraOnlyPerceptionProvider(lambda: b"jpeg")
        health = asyncio.run(provider.health())
        self.assertFalse(health.ready)
        self.assertEqual(health.backend, "camera-only")

    def test_yolo_provider_waits_for_camera_frame_before_loading_model(self) -> None:
        provider = YoloPerceptionProvider(lambda: None)
        health = asyncio.run(provider.health())
        self.assertFalse(health.ready)
        self.assertEqual(health.backend, "yolo")
        self.assertIn("no camera frame", health.detail)

    def test_observation_dict_normalizes_pixel_box(self) -> None:
        observation = Observation(
            timestamp=1.0,
            frame_available=True,
            frame_width=640,
            frame_height=480,
            detections=[Detection("person", 0.9, x=320, y=240, width=160, height=240)],
        )
        box = observation.to_dict()["detections"][0]["box"]  # type: ignore[index]
        self.assertAlmostEqual(box["left"], 0.375)  # type: ignore[index]
        self.assertAlmostEqual(box["top"], 0.25)  # type: ignore[index]
        self.assertAlmostEqual(box["width"], 0.25)  # type: ignore[index]
        self.assertAlmostEqual(box["height"], 0.5)  # type: ignore[index]

    def test_best_human_detection_ignores_other_objects(self) -> None:
        observation = Observation(
            timestamp=1.0,
            frame_available=True,
            detections=[Detection("chair", 0.99), Detection("person", 0.75), Detection("person", 0.85)],
        )
        self.assertEqual(best_human_detection(observation).confidence, 0.85)  # type: ignore[union-attr]


class FollowControllerTests(unittest.TestCase):
    def test_follow_turns_toward_off_center_person(self) -> None:
        client = FakeClient()
        controller = HumanFollowController(client)
        observation = Observation(
            timestamp=1.0,
            frame_available=True,
            frame_width=640,
            frame_height=480,
            detections=[Detection("person", 0.9, x=480, y=240, width=90, height=120)],
        )
        command = controller.plan(observation)
        self.assertLess(command.vyaw, 0.0)
        self.assertGreater(command.vx, 0.0)
        self.assertIn("person", controller.last_target)

    def test_follow_backs_away_from_close_person(self) -> None:
        client = FakeClient()
        controller = HumanFollowController(client)
        observation = Observation(
            timestamp=1.0,
            frame_available=True,
            frame_width=640,
            frame_height=480,
            detections=[Detection("person", 0.9, x=320, y=240, width=400, height=420)],
        )
        command = controller.plan(observation)
        self.assertLess(command.vx, 0.0)
        self.assertEqual(command.vyaw, 0.0)

    def test_follow_scans_when_sound_heard_without_person(self) -> None:
        client = FakeClient()
        controller = HumanFollowController(client)
        observation = Observation(timestamp=1.0, frame_available=True)
        command = controller.plan(observation, SoundCue(timestamp=9999999999.0, level=0.4))
        self.assertEqual(command.vx, 0.0)
        self.assertGreater(command.vyaw, 0.0)
        self.assertEqual(controller.last_target, "sound")


if __name__ == "__main__":
    unittest.main()
