"""Tests for first-pass autonomy helpers."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from go2_local_brain.autonomy.follow import HumanFollowController, SoundCue
from go2_local_brain.autonomy.lidar_map import LidarLocalMapper, LidarObstacleField, LidarTransform, lidar_debug_payload
from go2_local_brain.autonomy.local_map import LocalMapState, Pose2D, normalize_radians, raw_pose_from_sport_state
from go2_local_brain.autonomy.map import PatrolMap, Waypoint, list_patrol_maps, load_patrol_map, save_patrol_map
from go2_local_brain.autonomy.navigator import AutonomyNavigator
from go2_local_brain.autonomy.perception import (
    CameraOnlyPerceptionProvider,
    Detection,
    Observation,
    PerceptionHealth,
    PerceptionProvider,
    YoloPerceptionProvider,
    best_human_detection,
)
from go2_local_brain.autonomy.route_learning import PathRunRecorder
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

    async def health(self) -> PerceptionHealth:
        return PerceptionHealth(True, "test", "ready")


class SequencePerception(PerceptionProvider):
    def __init__(self, observations: list[Observation], health: PerceptionHealth | None = None) -> None:
        self.observations = observations
        self.index = 0
        self._health = health or PerceptionHealth(True, "yolo", "ready")

    async def observe(self) -> Observation:
        observation = self.observations[min(self.index, len(self.observations) - 1)]
        self.index += 1
        return observation

    async def health(self) -> PerceptionHealth:
        return self._health


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

    def test_saved_map_includes_localization_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = save_patrol_map(_map(), td)
            raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["metadata"]["coordinate_frame"], "local_odometry_m")
        self.assertTrue(raw["metadata"]["localization_required"])


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

    def test_local_map_staleness_and_trail_skip(self) -> None:
        state = LocalMapState(min_trail_step_m=0.5)
        state.update_from_sport_state({"position": [0.0, 0.0], "imu_state": {"rpy": [0.0, 0.0, 0.0]}}, now=10.0)
        state.update_from_sport_state({"position": [0.1, 0.0], "imu_state": {"rpy": [0.0, 0.0, 0.0]}}, now=10.1)
        self.assertEqual(len(state.trail), 1)
        self.assertTrue(state.is_fresh(now=10.2))
        self.assertFalse(state.is_fresh(now=12.0))

    def test_local_map_can_lock_startup_pose_to_saved_map_pose(self) -> None:
        state = LocalMapState()
        ok = state.lock_to_map_pose(
            Pose2D(5.0, 2.0, 0.0),
            {"position": [10.0, 10.0], "imu_state": {"rpy": [0.0, 0.0, 0.0]}},
            now=1.0,
        )
        self.assertTrue(ok)
        pose = state.update_from_sport_state(
            {"position": [11.0, 10.0], "imu_state": {"rpy": [0.0, 0.0, 0.0]}},
            now=2.0,
        )
        self.assertIsNotNone(pose)
        assert pose is not None
        self.assertAlmostEqual(pose.x, 6.0)
        self.assertAlmostEqual(pose.y, 2.0)


class LidarMapTests(unittest.TestCase):
    def test_lidar_obstacle_field_reports_front_and_clear_side(self) -> None:
        field = LidarObstacleField()
        summary = field.update([[0.45, 0.0, 0.0], [1.2, 0.6, 0.0], [2.0, -0.4, 0.0]], now=1.0)
        self.assertTrue(summary.fresh)
        self.assertAlmostEqual(summary.front_m, 0.45)
        self.assertLess(field.recommended_avoidance_turn(), 0.0)

    def test_lidar_mapper_projects_robot_points_into_map_frame(self) -> None:
        mapper = LidarLocalMapper(cell_size_m=0.5)
        mapper.add_scan(Pose2D(1.0, 2.0, 0.0), [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], now=1.0)
        payload = mapper.to_dict()
        self.assertEqual(payload["cell_count"], 2)
        cells = {(cell["x"], cell["y"]) for cell in payload["cells"]}  # type: ignore[index]
        self.assertIn((2.0, 2.0), cells)
        self.assertIn((1.0, 3.0), cells)

    def test_lidar_transform_rotates_and_flips_points(self) -> None:
        transform = LidarTransform(rotate_deg=90.0, flip_x=True)
        point = transform.apply([[1.0, 0.0, 0.0]])[0]
        self.assertAlmostEqual(point[0], 0.0, places=3)
        self.assertAlmostEqual(point[1], -1.0, places=3)

    def test_lidar_debug_payload_reports_parse_rate_and_bounds(self) -> None:
        payload = {
            "robot_points": [[1.0, 0.0, 0.0], [0.0, 2.0, 0.5]],
            "source_point_count": 2,
            "bounds": {"min_x": 0.0},
        }
        debug = lidar_debug_payload(
            raw_messages=10,
            parsed_messages=7,
            parse_errors=3,
            latest_payload=payload,
            latest_ts=8.0,
            transform=LidarTransform(rotate_deg=90.0),
            now=10.0,
        )
        self.assertEqual(debug["parse_error_rate"], 0.3)
        self.assertEqual(debug["point_count"], 2)
        self.assertEqual(debug["age_s"], 2.0)
        self.assertIsNotNone(debug["transformed_bounds"])

    def test_run_recorder_averages_repeated_paths(self) -> None:
        recorder = PathRunRecorder(min_step_m=0.0, min_period_s=0.0)
        recorder.start("a", now=1.0)
        recorder.add_pose(Pose2D(0.0, 0.0, 0.0), now=1.0)
        recorder.add_pose(Pose2D(1.0, 0.0, 0.0), now=2.0)
        recorder.stop(now=3.0)
        recorder.start("b", now=4.0)
        recorder.add_pose(Pose2D(0.0, 1.0, 0.0), now=4.0)
        recorder.add_pose(Pose2D(1.0, 1.0, 0.0), now=5.0)
        recorder.stop(now=6.0)
        averaged = recorder.average_path(points=2)
        self.assertEqual(averaged[0]["x"], 0.0)
        self.assertEqual(averaged[0]["y"], 0.5)
        self.assertEqual(averaged[1]["x"], 1.0)
        self.assertEqual(averaged[1]["y"], 0.5)


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

    def test_navigator_uses_lidar_front_obstacle_before_waypoint_drive(self) -> None:
        client = FakeClient()
        field = LidarObstacleField()
        field.update([[0.4, 0.0, 0.0], [2.0, -0.5, 0.0]], now=9999999999.0)
        navigator = AutonomyNavigator(client, lidar_obstacles=field)
        result = asyncio.run(navigator.move_toward(Waypoint("target", 1.0, 0.0)))
        self.assertIn("lidar avoid", result)
        self.assertLess(client.moves[-1][0], 0.0)


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
        self.assertEqual(controller.last_command.reason, command.reason)

    def test_follow_smooths_rapid_target_shift(self) -> None:
        client = FakeClient()
        controller = HumanFollowController(client)
        first = Observation(
            timestamp=1.0,
            frame_available=True,
            frame_width=640,
            frame_height=480,
            detections=[Detection("person", 0.9, x=480, y=240, width=90, height=120)],
        )
        second = Observation(
            timestamp=2.0,
            frame_available=True,
            frame_width=640,
            frame_height=480,
            detections=[Detection("person", 0.9, x=160, y=240, width=90, height=120)],
        )
        controller.plan(first)
        command = controller.plan(second)
        self.assertLess(abs(command.vyaw), 0.55)

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

    @unittest.skipIf(importlib.util.find_spec("aiohttp") is None, "aiohttp not installed")
    def test_gui_follow_step_observes_fresh_frame(self) -> None:
        from go2_local_brain.ai_autonomy_gui import AiAutonomyGui

        client = FakeClient()
        gui = _test_gui(AiAutonomyGui, detector="yolo")
        gui._follow = HumanFollowController(client)
        gui._latest_observation = Observation(timestamp=1.0, frame_available=True)
        gui._perception = SequencePerception(
            [
                Observation(
                    timestamp=2.0,
                    frame_available=True,
                    frame_width=640,
                    frame_height=480,
                    detections=[Detection("person", 0.9, x=480, y=240, width=90, height=120)],
                )
            ]
        )
        command = asyncio.run(gui._follow_step())
        self.assertIsNotNone(command)
        assert command is not None
        self.assertIn("person", gui._follow.last_target)
        self.assertLess(command.vyaw, 0.0)
        self.assertEqual(gui._latest_observation.timestamp, 2.0)

    @unittest.skipIf(importlib.util.find_spec("aiohttp") is None, "aiohttp not installed")
    def test_gui_follow_start_rejects_camera_only_detection(self) -> None:
        from go2_local_brain.ai_autonomy_gui import AiAutonomyGui

        gui = _test_gui(AiAutonomyGui, detector="camera")
        gui._perception = SequencePerception([Observation(timestamp=1.0, frame_available=True)])
        error = asyncio.run(gui._follow_start_error())
        self.assertIn("--detector yolo", error)


def _test_gui(gui_cls: type, *, detector: str) -> object:
    return gui_cls(
        "127.0.0.1",
        0,
        Path("."),
        None,
        False,
        detector,
        "yolov8n.pt",
        0.55,
        "",
        False,
        "visual",
    )


if __name__ == "__main__":
    unittest.main()
