"""Integration tests for identity-filtered local person following."""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

from go2_local_brain.autonomy.perception import Detection, Observation
from go2_local_brain.local_cockpit import LocalCockpit


class _FakeFollow:
    def __init__(self) -> None:
        self.observation = None

    async def step(self, observation):
        self.observation = observation
        return SimpleNamespace(reason="follow person forward")


class NamedFollowTests(unittest.TestCase):
    def test_named_follow_only_receives_matching_body(self) -> None:
        cockpit = LocalCockpit("127.0.0.1", 0)
        now = time.time()
        cooper = Detection("person", 0.9, x=160, y=240, width=160, height=400)
        other = Detection("person", 0.8, x=480, y=240, width=160, height=400)
        cockpit._latest_observation = Observation(now, True, [cooper, other], 640, 480)
        cockpit._person_tracker.update_bodies([cooper, other], 640, 480, now=now)
        cockpit._person_tracker.observe_faces(
            [{"label": "Cooper", "score": 0.9, "x": 0.20, "y": 0.14, "w": 0.08, "h": 0.12}],
            now=now,
        )
        follower = _FakeFollow()
        cockpit._follow = follower
        cockpit._follow_identity = "Cooper"
        asyncio.run(cockpit._follow_step())
        self.assertEqual(follower.observation.detections, [cooper])

    def test_missing_named_target_is_not_replaced_by_someone_else(self) -> None:
        cockpit = LocalCockpit("127.0.0.1", 0)
        now = time.time()
        other = Detection("person", 0.9, x=320, y=240, width=160, height=400)
        cockpit._latest_observation = Observation(now, True, [other], 640, 480)
        cockpit._person_tracker.update_bodies([other], 640, 480, now=now)
        follower = _FakeFollow()
        cockpit._follow = follower
        cockpit._follow_identity = "Cooper"
        asyncio.run(cockpit._follow_step())
        self.assertEqual(follower.observation.detections, [])
        self.assertEqual(cockpit._follow_last_action, "searching for Cooper")


if __name__ == "__main__":
    unittest.main()
