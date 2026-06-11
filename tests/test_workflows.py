"""Tests for the workflow engine + built-in workflows (no hardware)."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Optional

from go2_local_brain.autonomy.perception import Detection, Observation
from go2_local_brain.autonomy.workflows import (
    Step,
    Workflow,
    WorkflowContext,
    WorkflowEngine,
    builtin_workflows,
)


class FakeRobot:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def move(self, vx=0.0, vy=0.0, vyaw=0.0, duration_s=0.0) -> None:
        self.calls.append(f"move({vx},{vy},{vyaw},{duration_s})")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def stand_up(self) -> None:
        self.calls.append("stand_up")

    async def sit_down(self) -> None:
        self.calls.append("sit_down")

    async def advanced_action(self, name: str) -> None:
        self.calls.append(f"action:{name}")

    async def explore_room(self, duration_s, mode=None) -> None:
        self.calls.append(f"explore({duration_s},{mode})")


class FakeTrack:
    def __init__(self, label: str, known: bool) -> None:
        self.label = label
        self.is_known = known


def _ctx(robot, **kw) -> WorkflowContext:
    return WorkflowContext(robot=robot, **kw)


class StepExecutionTests(unittest.TestCase):
    def test_basic_steps_run_in_order(self) -> None:
        async def run() -> None:
            robot = FakeRobot()
            eng = WorkflowEngine(_ctx(robot))
            eng.register(Workflow("t", "test", steps=[
                Step("stand"),
                Step("move", {"vx": 0.2, "duration_s": 0.1}),
                Step("sit"),
            ]))
            await eng.start("t")
            # Let it finish.
            for _ in range(50):
                if not eng.is_running():
                    break
                await asyncio.sleep(0.02)
            self.assertIn("stand_up", robot.calls)
            self.assertIn("sit_down", robot.calls)
            self.assertEqual(eng.status().state, "done")

        asyncio.run(run())

    def test_unknown_workflow_returns_false(self) -> None:
        async def run() -> None:
            eng = WorkflowEngine(_ctx(FakeRobot()))
            self.assertFalse(await eng.start("nope"))

        asyncio.run(run())

    def test_stop_halts_a_loop(self) -> None:
        async def run() -> None:
            robot = FakeRobot()
            eng = WorkflowEngine(_ctx(robot))
            eng.register(Workflow("spin", "test", steps=[
                Step("loop", {"count": 0, "steps": [
                    {"kind": "move", "params": {"vyaw": 0.5, "duration_s": 0.05}},
                ]}),
            ]))
            await eng.start("spin")
            await asyncio.sleep(0.15)
            self.assertTrue(eng.is_running())
            await eng.stop()
            self.assertFalse(eng.is_running())
            self.assertIn("stop", robot.calls)

        asyncio.run(run())

    def test_greet_if_known_greets_only_for_known(self) -> None:
        async def run() -> None:
            robot = FakeRobot()
            # Known face present.
            eng = WorkflowEngine(_ctx(robot, face_tracks=lambda: [FakeTrack("cooper", True)]))
            eng.register(Workflow("g", "t", steps=[Step("greet_if_known")]))
            await eng.start("g")
            for _ in range(50):
                if not eng.is_running():
                    break
                await asyncio.sleep(0.02)
            self.assertIn("action:greet", robot.calls)

        asyncio.run(run())

    def test_greet_if_known_skips_when_unknown(self) -> None:
        async def run() -> None:
            robot = FakeRobot()
            eng = WorkflowEngine(_ctx(robot, face_tracks=lambda: [FakeTrack("unknown", False)]))
            eng.register(Workflow("g", "t", steps=[Step("greet_if_known")]))
            await eng.start("g")
            for _ in range(50):
                if not eng.is_running():
                    break
                await asyncio.sleep(0.02)
            self.assertNotIn("action:greet", robot.calls)

        asyncio.run(run())

    def test_scan_for_person_stops_when_found(self) -> None:
        async def run() -> None:
            robot = FakeRobot()
            person_obs = Observation(
                timestamp=0.0, frame_available=True,
                detections=[Detection(label="person", confidence=0.9, x=320, y=240, width=100, height=200)],
                frame_width=640, frame_height=480,
            )

            async def observe() -> Optional[Observation]:
                return person_obs

            eng = WorkflowEngine(_ctx(robot, observe=observe))
            eng.register(Workflow("f", "t", steps=[Step("scan_for_person", {"max_turns": 5})]))
            await eng.start("f")
            for _ in range(50):
                if not eng.is_running():
                    break
                await asyncio.sleep(0.02)
            self.assertIn("stop", robot.calls)

        asyncio.run(run())


class TargetingStepTests(unittest.TestCase):
    def test_targeting_step_uses_controller(self) -> None:
        async def run() -> None:
            robot = FakeRobot()

            class FakeDecision:
                has_target = True
                locked = True
                fired = True
                aim_vyaw = 0.3
                reason = "fired"
                target_label = "person"

            class FakeTargeting:
                def __init__(self) -> None:
                    self.steps = 0

                async def step(self, obs, now=None):
                    self.steps += 1
                    return FakeDecision()

            targeting = FakeTargeting()
            obs = Observation(timestamp=0.0, frame_available=True, detections=[], frame_width=640, frame_height=480)

            async def observe():
                return obs

            eng = WorkflowEngine(_ctx(robot, observe=observe, targeting=targeting))
            eng.register(Workflow("pt", "t", steps=[Step("targeting", {"duration_s": 0.3, "tick_s": 0.05})]))
            await eng.start("pt")
            for _ in range(60):
                if not eng.is_running():
                    break
                await asyncio.sleep(0.02)
            self.assertGreaterEqual(targeting.steps, 1)
            # aim_vyaw nonzero -> a move was issued.
            self.assertTrue(any(c.startswith("move") for c in robot.calls))

        asyncio.run(run())


class BuiltinWorkflowTests(unittest.TestCase):
    def test_all_builtins_registered(self) -> None:
        eng = WorkflowEngine(_ctx(FakeRobot()))
        names = {w["name"] for w in eng.list_workflows()}
        self.assertEqual(names, {"patrol_and_greet", "find_person", "guard_post", "phone_tracker"})

    def test_builtins_have_steps(self) -> None:
        for wf in builtin_workflows():
            self.assertTrue(wf.steps, f"{wf.name} has no steps")


if __name__ == "__main__":
    unittest.main()
