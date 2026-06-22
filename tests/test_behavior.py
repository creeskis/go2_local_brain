"""Tests for the headless roam+follow behaviour logic (no hardware)."""

from __future__ import annotations

import unittest

from go2_local_brain.autonomy.behavior import (
    MODE_FOLLOW,
    MODE_ROAM,
    MODE_SCAN,
    gate_follow_with_lidar,
    select_mode,
)
from go2_local_brain.autonomy.lidar_map import LidarObstacleSummary


def _summary(*, front=None, left=None, right=None, fresh=True) -> LidarObstacleSummary:
    return LidarObstacleSummary(
        point_count=200, front_m=front, left_m=left, right_m=right, rear_m=None,
        fresh=fresh, age_s=0.0,
    )


class SelectModeTests(unittest.TestCase):
    def test_person_visible_follows(self) -> None:
        self.assertEqual(select_mode(True, 0.0, follow_grace_s=2.0), MODE_FOLLOW)

    def test_recently_lost_scans(self) -> None:
        self.assertEqual(select_mode(False, 1.0, follow_grace_s=2.0), MODE_SCAN)

    def test_long_lost_roams(self) -> None:
        self.assertEqual(select_mode(False, 5.0, follow_grace_s=2.0), MODE_ROAM)

    def test_grace_boundary_is_scan(self) -> None:
        self.assertEqual(select_mode(False, 2.0, follow_grace_s=2.0), MODE_SCAN)


class GateFollowTests(unittest.TestCase):
    def test_forward_into_obstacle_is_redirected_to_open_side(self) -> None:
        g = gate_follow_with_lidar(
            0.6, 0.0, 0.45, _summary(front=0.3, left=2.0, right=0.5),
            stop_distance_m=0.55, turn_rate_rps=0.85,
        )
        self.assertEqual(g.vx, 0.0)
        self.assertGreater(g.vyaw, 0.0)  # turn left toward the open side

    def test_clear_front_passes_through(self) -> None:
        g = gate_follow_with_lidar(
            0.6, 0.1, 0.45, _summary(front=3.0, left=3.0, right=3.0),
            stop_distance_m=0.55, turn_rate_rps=0.85,
        )
        self.assertEqual(g.vx, 0.6)
        self.assertEqual(g.vyaw, 0.1)

    def test_backing_up_is_not_gated(self) -> None:
        g = gate_follow_with_lidar(
            -0.3, 0.2, 0.45, _summary(front=0.2, left=1.0, right=1.0),
            stop_distance_m=0.55, turn_rate_rps=0.85,
        )
        self.assertEqual(g.vx, -0.3)  # already moving away; leave it

    def test_stale_lidar_does_not_gate(self) -> None:
        g = gate_follow_with_lidar(
            0.6, 0.0, 0.45, _summary(front=0.3, fresh=False),
            stop_distance_m=0.55, turn_rate_rps=0.85,
        )
        self.assertEqual(g.vx, 0.6)  # can't trust stale LiDAR to override follow


if __name__ == "__main__":
    unittest.main()
