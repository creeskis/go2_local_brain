"""Tests for phone-user targeting + Nerf safety gates (no hardware)."""

from __future__ import annotations

import asyncio
import unittest

from go2_local_brain.autonomy.perception import Detection, Observation
from go2_local_brain.autonomy.targeting import (
    LoggingNerfController,
    TargetingController,
    TargetingTuning,
    build_nerf_controller,
    find_phone_users,
)


def _obs(detections, fw=640, fh=480) -> Observation:
    return Observation(timestamp=0.0, frame_available=True, detections=detections, frame_width=fw, frame_height=fh)


def _person(x, y, w=120, h=300) -> Detection:
    return Detection(label="person", confidence=0.9, x=x, y=y, width=w, height=h)


def _phone(x, y, w=30, h=60) -> Detection:
    return Detection(label="cell phone", confidence=0.8, x=x, y=y, width=w, height=h)


class FindPhoneUsersTests(unittest.TestCase):
    def test_phone_inside_person_is_a_user(self) -> None:
        users = find_phone_users(_obs([_person(320, 240), _phone(320, 260)]))
        self.assertEqual(len(users), 1)
        self.assertAlmostEqual(users[0].center_x_norm, 0.5, places=2)

    def test_phone_outside_any_person_ignored(self) -> None:
        users = find_phone_users(_obs([_person(100, 240), _phone(600, 100)]))
        self.assertEqual(users, [])

    def test_no_phone_no_users(self) -> None:
        self.assertEqual(find_phone_users(_obs([_person(320, 240)])), [])

    def test_users_sorted_by_centeredness(self) -> None:
        # Two phone users; the more-centered one comes first.
        dets = [
            _person(100, 240), _phone(100, 260),   # far left
            _person(330, 240), _phone(330, 260),   # near center
        ]
        users = find_phone_users(_obs(dets))
        self.assertEqual(len(users), 2)
        self.assertLess(abs(users[0].horizontal_error), abs(users[1].horizontal_error))

    def test_tightest_person_wins_as_holder(self) -> None:
        # A big background person and a tight foreground person both contain
        # the phone; the tighter (smaller-area) one is the holder.
        big = Detection(label="person", confidence=0.9, x=320, y=240, width=600, height=460)
        tight = _person(320, 240, w=120, h=300)
        users = find_phone_users(_obs([big, tight, _phone(320, 250)]))
        self.assertEqual(len(users), 1)
        self.assertIs(users[0].person, tight)


class NerfControllerTests(unittest.TestCase):
    def test_disarmed_does_not_fire(self) -> None:
        nerf = LoggingNerfController()
        fired = asyncio.run(nerf.fire())
        self.assertFalse(fired)
        self.assertFalse(nerf.armed)

    def test_armed_logging_fires(self) -> None:
        nerf = LoggingNerfController()
        nerf.arm()
        self.assertTrue(asyncio.run(nerf.fire()))
        self.assertEqual(nerf.status()["shots"], 1)

    def test_factory_default_is_logging(self) -> None:
        self.assertIsInstance(build_nerf_controller("logging"), LoggingNerfController)

    def test_factory_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_nerf_controller("laser")


class TargetingControllerTests(unittest.TestCase):
    def _centered(self):
        # Phone user dead-center.
        return _obs([_person(320, 240), _phone(320, 260)])

    def test_no_target_no_fire(self) -> None:
        ctrl = TargetingController()
        decision = asyncio.run(ctrl.step(_obs([])))
        self.assertFalse(decision.has_target)
        self.assertFalse(decision.fired)

    def test_locks_then_refuses_fire_when_disarmed(self) -> None:
        ctrl = TargetingController(tuning=TargetingTuning(lock_frames=2))
        asyncio.run(ctrl.step(self._centered()))
        decision = asyncio.run(ctrl.step(self._centered()))
        self.assertTrue(decision.locked)
        self.assertFalse(decision.fired)
        self.assertIn("disarmed", decision.reason)

    def test_fires_when_armed_locked_and_centered(self) -> None:
        ctrl = TargetingController(tuning=TargetingTuning(lock_frames=2, cooldown_s=0.0))
        ctrl.arm()
        asyncio.run(ctrl.step(self._centered()))
        decision = asyncio.run(ctrl.step(self._centered()))
        self.assertTrue(decision.fired)

    def test_cooldown_blocks_rapid_fire(self) -> None:
        ctrl = TargetingController(tuning=TargetingTuning(lock_frames=1, cooldown_s=100.0))
        ctrl.arm()
        first = asyncio.run(ctrl.step(self._centered(), now=0.0))
        self.assertTrue(first.fired)
        # Re-lock then try again within cooldown.
        asyncio.run(ctrl.step(self._centered(), now=0.1))
        second = asyncio.run(ctrl.step(self._centered(), now=0.2))
        self.assertFalse(second.fired)
        self.assertIn("cooldown", second.reason)

    def test_session_cap_enforced(self) -> None:
        ctrl = TargetingController(tuning=TargetingTuning(lock_frames=1, cooldown_s=0.0, session_fire_cap=1))
        ctrl.arm()
        t = 0.0
        fired_count = 0
        for _ in range(6):
            d = asyncio.run(ctrl.step(self._centered(), now=t))
            if d.fired:
                fired_count += 1
            t += 1.0
        self.assertEqual(fired_count, 1)

    def test_off_center_target_aims_not_fires(self) -> None:
        ctrl = TargetingController(tuning=TargetingTuning(lock_frames=1, center_tolerance=0.05))
        ctrl.arm()
        # Phone user far left -> large aim, never locks.
        obs = _obs([_person(60, 240), _phone(60, 260)])
        decision = asyncio.run(ctrl.step(obs))
        self.assertTrue(decision.has_target)
        self.assertFalse(decision.fired)
        self.assertNotEqual(decision.aim_vyaw, 0.0)


if __name__ == "__main__":
    unittest.main()
