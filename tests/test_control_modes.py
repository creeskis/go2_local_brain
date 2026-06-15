"""Tests for the keyboard control resolver (Feature 1)."""

from __future__ import annotations

import unittest

from go2_local_brain.autonomy.control_modes import (
    ControlMode,
    SpeedLevel,
    mode_enter_action,
    next_speed,
    resolve_held,
    resolve_press,
    speed_scale,
)
from go2_local_brain.safety.limits import MAX_VX, MAX_VY, MAX_VYAW


class SpeedTests(unittest.TestCase):
    def test_speed_cycle(self) -> None:
        self.assertEqual(next_speed(SpeedLevel.SLOW), SpeedLevel.NORMAL)
        self.assertEqual(next_speed(SpeedLevel.NORMAL), SpeedLevel.FAST)
        self.assertEqual(next_speed(SpeedLevel.FAST), SpeedLevel.SLOW)

    def test_scales_ordered_and_under_one(self) -> None:
        s = [speed_scale(x) for x in (SpeedLevel.SLOW, SpeedLevel.NORMAL, SpeedLevel.FAST)]
        self.assertEqual(s, sorted(s))
        self.assertLess(s[-1], 1.0)  # even fast stays inside the hard clamps


class NormalModeHeldTests(unittest.TestCase):
    def test_forward(self) -> None:
        cmd = resolve_held(ControlMode.NORMAL, SpeedLevel.NORMAL, {"w"})
        self.assertEqual(cmd.kind, "velocity")
        self.assertGreater(cmd.vx, 0.0)
        self.assertEqual(cmd.vy, 0.0)
        self.assertEqual(cmd.vyaw, 0.0)

    def test_q_e_are_turns(self) -> None:
        left = resolve_held(ControlMode.NORMAL, SpeedLevel.NORMAL, {"q"})
        right = resolve_held(ControlMode.NORMAL, SpeedLevel.NORMAL, {"e"})
        self.assertGreater(left.vyaw, 0.0)   # Q = turn left (CCW, +yaw)
        self.assertLess(right.vyaw, 0.0)     # E = turn right (CW, -yaw)

    def test_diagonal_combines(self) -> None:
        cmd = resolve_held(ControlMode.NORMAL, SpeedLevel.NORMAL, {"w", "a"})
        self.assertGreater(cmd.vx, 0.0)
        self.assertGreater(cmd.vy, 0.0)      # A = strafe left (+vy)

    def test_no_keys_is_stop(self) -> None:
        self.assertEqual(resolve_held(ControlMode.NORMAL, SpeedLevel.NORMAL, set()).kind, "stop")

    def test_opposite_keys_cancel_to_stop(self) -> None:
        self.assertEqual(resolve_held(ControlMode.NORMAL, SpeedLevel.NORMAL, {"w", "s"}).kind, "stop")

    def test_fast_under_clamp(self) -> None:
        cmd = resolve_held(ControlMode.NORMAL, SpeedLevel.FAST, {"w", "a", "q"})
        self.assertLessEqual(abs(cmd.vx), MAX_VX)
        self.assertLessEqual(abs(cmd.vy), MAX_VY)
        self.assertLessEqual(abs(cmd.vyaw), MAX_VYAW)

    def test_non_normal_held_is_noop(self) -> None:
        for mode in (ControlMode.FLIP, ControlMode.JUMP, ControlMode.BACKSTAND):
            self.assertEqual(resolve_held(mode, SpeedLevel.NORMAL, {"w"}).kind, "noop")


class FlipModeTests(unittest.TestCase):
    def test_four_directions(self) -> None:
        self.assertEqual(resolve_press(ControlMode.FLIP, "w").action, "front_flip")
        self.assertEqual(resolve_press(ControlMode.FLIP, "s").action, "back_flip")
        self.assertEqual(resolve_press(ControlMode.FLIP, "a").action, "left_flip")
        self.assertEqual(resolve_press(ControlMode.FLIP, "d").action, "right_flip")

    def test_flip_command_kind(self) -> None:
        self.assertEqual(resolve_press(ControlMode.FLIP, "w").kind, "action")

    def test_unmapped_key_noop(self) -> None:
        self.assertEqual(resolve_press(ControlMode.FLIP, "z").kind, "noop")


class JumpModeTests(unittest.TestCase):
    def test_forward_only(self) -> None:
        self.assertEqual(resolve_press(ControlMode.JUMP, "w").action, "jump")
        self.assertEqual(resolve_press(ControlMode.JUMP, "a").kind, "noop")
        self.assertEqual(resolve_press(ControlMode.JUMP, "s").kind, "noop")


class BackstandModeTests(unittest.TestCase):
    def test_wasd_disabled(self) -> None:
        self.assertEqual(resolve_press(ControlMode.BACKSTAND, "w").kind, "noop")

    def test_enter_action_is_backstand(self) -> None:
        self.assertEqual(mode_enter_action(ControlMode.BACKSTAND), "backstand")

    def test_other_modes_enter_balance_stand(self) -> None:
        for mode in (ControlMode.NORMAL, ControlMode.FLIP, ControlMode.JUMP):
            self.assertEqual(mode_enter_action(mode), "balance_stand")


class SpaceStopTests(unittest.TestCase):
    def test_space_stops_in_any_mode(self) -> None:
        for mode in ControlMode:
            self.assertEqual(resolve_press(mode, " ").kind, "stop")


if __name__ == "__main__":
    unittest.main()
