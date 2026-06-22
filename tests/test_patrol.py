"""Tests for the headless LiDAR patrol planner (no hardware)."""

from __future__ import annotations

import unittest

from go2_local_brain.autonomy.lidar_map import LidarObstacleSummary
from go2_local_brain.autonomy.patrol import (
    PatrolController,
    PatrolParams,
    plan_patrol_step,
)


def _summary(
    *, front=None, left=None, right=None, rear=None, point_count=200, fresh=True
) -> LidarObstacleSummary:
    return LidarObstacleSummary(
        point_count=point_count,
        front_m=front,
        left_m=left,
        right_m=right,
        rear_m=rear,
        fresh=fresh,
        age_s=0.0,
    )


class PlanStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.params = PatrolParams()

    def test_clear_front_cruises_forward(self) -> None:
        d = plan_patrol_step(_summary(front=3.0, left=3.0, right=3.0), self.params)
        self.assertEqual(d.action, "forward")
        self.assertAlmostEqual(d.vx, self.params.forward_speed_mps)
        self.assertGreater(d.duration_s, 0.0)

    def test_no_returns_in_front_is_treated_as_clear(self) -> None:
        # front_m None -> open, so it should cruise, not hold.
        d = plan_patrol_step(_summary(front=None, left=2.0, right=2.0), self.params)
        self.assertEqual(d.action, "forward")

    def test_blocked_front_backs_up_and_turns_to_open_side(self) -> None:
        # Wall ahead, more room on the left -> turn left (positive yaw).
        d = plan_patrol_step(_summary(front=0.30, left=2.5, right=0.6), self.params)
        self.assertEqual(d.action, "avoid")
        self.assertLess(d.vx, 0.0)            # backing up
        self.assertGreater(d.vyaw, 0.0)       # turning left toward open space

    def test_blocked_front_turns_right_when_right_is_open(self) -> None:
        d = plan_patrol_step(_summary(front=0.30, left=0.6, right=2.5), self.params)
        self.assertEqual(d.action, "avoid")
        self.assertLess(d.vyaw, 0.0)          # turning right

    def test_caution_band_creeps_and_steers(self) -> None:
        d = plan_patrol_step(_summary(front=0.9, left=2.0, right=0.7), self.params)
        self.assertEqual(d.action, "steer")
        self.assertGreater(d.vx, 0.0)
        self.assertLess(d.vx, self.params.forward_speed_mps)  # slower than cruise

    def test_stale_lidar_holds_by_default(self) -> None:
        d = plan_patrol_step(_summary(front=3.0, fresh=False), self.params)
        self.assertEqual(d.action, "hold")
        self.assertEqual((d.vx, d.vy, d.vyaw), (0.0, 0.0, 0.0))
        self.assertFalse(d.moves)

    def test_no_points_holds_by_default(self) -> None:
        d = plan_patrol_step(_summary(front=None, point_count=0), self.params)
        self.assertEqual(d.action, "hold")

    def test_allow_blind_roams_without_lidar(self) -> None:
        params = PatrolParams(allow_blind=True)
        d = plan_patrol_step(_summary(point_count=0, fresh=False), params)
        self.assertEqual(d.action, "blind")
        self.assertGreater(d.vx, 0.0)


class PatrolControllerTests(unittest.TestCase):
    def test_repeated_avoids_lock_turn_direction(self) -> None:
        ctrl = PatrolController(PatrolParams())
        # First avoid picks left (more open). A later ambiguous frame should
        # keep turning left rather than flipping.
        ctrl.step(_summary(front=0.3, left=2.5, right=0.6))
        d = ctrl.step(_summary(front=0.3, left=1.0, right=1.0))  # tie
        self.assertGreater(d.vyaw, 0.0)

    def test_escape_pivot_after_being_stuck(self) -> None:
        params = PatrolParams(stuck_pivots=3)
        ctrl = PatrolController(params)
        actions = [ctrl.step(_summary(front=0.3, left=2.0, right=0.5)).action for _ in range(3)]
        self.assertEqual(actions[-1], "escape")

    def test_clear_run_resets_stuck_counter(self) -> None:
        params = PatrolParams(stuck_pivots=3)
        ctrl = PatrolController(params)
        ctrl.step(_summary(front=0.3, left=2.0, right=0.5))
        ctrl.step(_summary(front=0.3, left=2.0, right=0.5))
        ctrl.step(_summary(front=3.0, left=3.0, right=3.0))   # clear -> reset
        d = ctrl.step(_summary(front=0.3, left=2.0, right=0.5))
        self.assertEqual(d.action, "avoid")  # not escape; streak was reset


class PatrolParamsTests(unittest.TestCase):
    def test_rejects_speed_over_limit(self) -> None:
        with self.assertRaises(ValueError):
            PatrolParams(forward_speed_mps=99.0)

    def test_rejects_slow_below_stop(self) -> None:
        with self.assertRaises(ValueError):
            PatrolParams(stop_distance_m=1.0, slow_distance_m=0.5)

    def test_rejects_zero_stuck_pivots(self) -> None:
        with self.assertRaises(ValueError):
            PatrolParams(stuck_pivots=0)


if __name__ == "__main__":
    unittest.main()
