"""Tests for first-pass autonomy helpers."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from go2_local_brain.autonomy.map import PatrolMap, Waypoint, load_patrol_map
from go2_local_brain.autonomy.navigator import AutonomyNavigator
from go2_local_brain.autonomy.perception import Detection, Observation, PerceptionProvider
from go2_local_brain.autonomy.supervisor import AutonomySupervisor


class FakeClient:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float, float, float]] = []
        self.stops = 0

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
        self.assertIn("step toward target", status.last_action)

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
        self.assertGreaterEqual(len(client.moves), 2)

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


if __name__ == "__main__":
    unittest.main()
