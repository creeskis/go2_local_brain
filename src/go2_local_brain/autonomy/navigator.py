"""Small, interruptible navigation primitives for autonomy mode."""

from __future__ import annotations

import asyncio
import math
from typing import Protocol

from .map import Waypoint


class RobotMover(Protocol):
    async def move(self, vx: float, vy: float = 0.0, vyaw: float = 0.0, duration_s: float = 0.0) -> None: ...

    async def stop(self) -> None: ...


class AutonomyNavigator:
    """Approximate waypoint movement without claiming full localization."""

    def __init__(self, client: RobotMover) -> None:
        self._client = client

    async def move_toward(self, waypoint: Waypoint) -> str:
        """Take one short step toward a waypoint in map coordinates."""
        distance = math.hypot(waypoint.x, waypoint.y)
        if distance < 0.25:
            await self.scan()
            return f"scan at {waypoint.name}"

        yaw_error = math.atan2(waypoint.y, waypoint.x)
        if abs(yaw_error) > 0.35:
            turn = max(-0.55, min(0.55, yaw_error))
            await self._client.move(0.0, 0.0, turn, 0.35)
            return f"turn toward {waypoint.name}"

        step_duration = min(0.55, max(0.25, distance * 0.25))
        await self._client.move(0.25, 0.0, 0.0, step_duration)
        return f"step toward {waypoint.name}"

    async def scan(self) -> None:
        """Perform a small visual scan without committing to travel."""
        await self._client.move(0.0, 0.0, 0.45, 0.35)
        await asyncio.sleep(0.05)
        await self._client.move(0.0, 0.0, -0.45, 0.35)

    async def stop(self) -> None:
        await self._client.stop()
