"""Autonomy supervisor for map patrol and perception-driven behavior."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Literal

from .map import PatrolMap
from .navigator import AutonomyNavigator
from .perception import Observation, PerceptionProvider

AutonomyState = Literal["idle", "arming", "patrolling", "scanning", "investigating", "paused", "error_stop"]


@dataclass
class AutonomyStatus:
    """Snapshot for browser status and tests."""

    state: AutonomyState
    active: bool
    map_name: str
    current_waypoint: str | None
    route_index: int
    last_observation: str
    last_action: str
    events: list[str] = field(default_factory=list)


class AutonomySupervisor:
    """Runs an interruptible patrol loop independent from direct GUI controls."""

    def __init__(
        self,
        patrol_map: PatrolMap,
        navigator: AutonomyNavigator,
        perception: PerceptionProvider,
        *,
        tick_s: float = 0.15,
        max_events: int = 80,
    ) -> None:
        self._map = patrol_map
        self._navigator = navigator
        self._perception = perception
        self._tick_s = tick_s
        self._max_events = max_events
        self._state: AutonomyState = "idle"
        self._route_index = 0
        self._last_observation = "none"
        self._last_action = "none"
        self._events: list[str] = []
        self._task: asyncio.Task[None] | None = None

    async def activate(self) -> None:
        if self._task is not None and not self._task.done():
            self._event("activate ignored; already running")
            return
        self._state = "arming"
        self._event("AI-only autonomy armed")
        self._task = asyncio.create_task(self._run(), name="go2-ai-autonomy")

    async def pause(self) -> None:
        if self._state in {"idle", "error_stop"}:
            return
        self._state = "paused"
        await self._navigator.stop()
        self._event("paused")

    async def resume(self) -> None:
        if self._state != "paused":
            return
        self._state = "patrolling"
        self._event("resumed")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._navigator.stop()
        self._state = "idle"
        self._last_action = "stop"
        self._event("stopped")

    def status(self) -> AutonomyStatus:
        waypoint = None
        if self._map.patrol_route:
            waypoint = self._map.patrol_route[self._route_index % len(self._map.patrol_route)]
        return AutonomyStatus(
            state=self._state,
            active=self._task is not None and not self._task.done(),
            map_name=self._map.name,
            current_waypoint=waypoint,
            route_index=self._route_index,
            last_observation=self._last_observation,
            last_action=self._last_action,
            events=list(self._events),
        )

    async def step_once(self) -> None:
        """Run one patrol decision. Useful for tests and future manual stepping."""
        if self._state == "paused":
            return
        if self._state == "arming":
            self._state = "patrolling"
            self._event("patrol started")

        observation = await self._perception.observe()
        self._last_observation = observation.summary()
        if _has_interesting_detection(observation):
            await self._investigate(observation)
            return

        # 1. Look up the active waypoint without changing the index yet
        _, waypoint = self._map.next_waypoint(self._route_index)
        self._state = "patrolling"
        
        # 2. Take a step toward the locked waypoint
        action_result = await self._navigator.move_toward(waypoint)
        self._last_action = action_result
        self._event(f"{self._last_action}; obs={self._last_observation}")
        
        # 3. ONLY increment to the next waypoint if the navigator says it arrived and scanned!
        if "scan" in action_result:
            self._route_index = (self._route_index + 1) % len(self._map.patrol_route)
            
    async def _run(self) -> None:
        try:
            while True:
                await self.step_once()
                await asyncio.sleep(self._tick_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._state = "error_stop"
            self._last_action = f"error: {exc}"
            self._event(self._last_action)
            await self._navigator.stop()

    async def _investigate(self, observation: Observation) -> None:
        self._state = "investigating"
        
        # Build the string prefix the unit tests look for
        labels = ", ".join(d.label for d in observation.detections[:4])
        base_action = f"investigate {labels}"
        
        # Pull out the human target if one is in view
        human = None
        for d in observation.detections:
            if d.label == "person" or getattr(d, "kind", "") == "human":
                human = d
                break
                
        if human is not None and observation.frame_width:
            # Handle both raw pixel values or pre-normalized coordinates
            center_x = human.x / observation.frame_width if human.x > 1.0 else human.x
            center_error = center_x - 0.5
            
            # Apply our working negative angular gain to track smoothly
            if abs(center_error) > 0.10:
                turn = -center_error * 1.3
                turn = max(-0.45, min(0.45, turn)) # Clamp to comfortable turning speeds
                self._last_action = f"{base_action}: tracking human visual: turn {turn:.2f}"
                await self._navigator._client.move(0.0, 0.0, turn, 0.20)
            else:
                self._last_action = f"{base_action}: holding position watching human"
                await self._navigator._client.move(0.0, 0.0, 0.0, 0.20)
        else:
            # Fallback for abstract test triggers or blank frames
            self._last_action = f"{base_action}: standing alert"
            await self._navigator._client.move(0.0, 0.0, 0.0, 0.20)
            
        self._event(f"{self._last_action}; obs={observation.summary()}")
        self._state = "patrolling"
        
    def _event(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._events.append(f"{stamp} {message}")
        if len(self._events) > self._max_events:
            del self._events[: len(self._events) - self._max_events]


def _has_interesting_detection(observation: Observation) -> bool:
    """Check if an observation contains a tracking target."""
    # Live detector observations are consumed by follow mode; patrol stays on the map route.
    if "detector_backend" in getattr(observation, "note", ""):
        return False

    # Synthetic/test observations still trigger investigation behavior.
    return any(d.confidence >= 0.55 for d in observation.detections)
