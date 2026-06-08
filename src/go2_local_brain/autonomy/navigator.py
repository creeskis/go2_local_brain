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
    """Approximate waypoint movement using live odometry telemetry data."""

    def __init__(self, client: RobotMover) -> None:
        self._client = client

    async def move_toward(self, waypoint: Waypoint) -> str:
        """Take one smooth step toward a waypoint by blending forward and turning vectors."""
        # 1. Safely extract live coordinate position and heading from telemetry cache
        sport_state = getattr(self._client, "_sport_state", None) or getattr(self._client, "sport_state", None)
        
        current_x = 0.0
        current_y = 0.0
        current_yaw = 0.0
        range_obstacle = [0.0, 0.0, 0.0, 0.0]

        if sport_state and isinstance(sport_state, dict):
            pos = sport_state.get("position", [0.0, 0.0, 0.0])
            if len(pos) >= 2:
                current_x = pos[0]
                current_y = pos[1]
            
            imu = sport_state.get("imu_state", {})
            if isinstance(imu, dict):
                rpy = imu.get("rpy", [0.0, 0.0, 0.0])
                if len(rpy) >= 3:
                    current_yaw = rpy[2]
            
            range_obstacle = sport_state.get("range_obstacle", [0.0, 0.0, 0.0, 0.0])

        # 2. Calculate absolute world delta gaps
        dx_abs = waypoint.x - current_x
        dy_abs = waypoint.y - current_y

        # 3. Transform world coordinate offsets into the robot's local orientation frame
        cos_yaw = math.cos(current_yaw)
        sin_yaw = math.sin(current_yaw)
        dx_local = dx_abs * cos_yaw + dy_abs * sin_yaw
        dy_local = -dx_abs * sin_yaw + dy_abs * cos_yaw

        # 4. Compute true remaining distance
        distance = math.hypot(dx_local, dy_local)
        if distance < 0.25:
            await self.scan()
            return f"scan at {waypoint.name}"

        # 5. Compute true relative steering error
        yaw_error = math.atan2(dy_local, dx_local)

        # 5.5 LIVE OBSTACLE AVOIDANCE GUARD
        if len(range_obstacle) > 0 and 0.01 < range_obstacle[0] < 0.70:
            await self._client.move(-0.15, 0.0, 0.40, 0.40)
            return f"avoiding obstacle! object detected front: {range_obstacle[0]:.2f}m"

        # 6. BLENDED CONTROL LAW (Eradicates the turn-vs-walk hard switch glitch)
        # If facing completely away (more than 45 degrees / 0.8 rad), pivot in place first
        if abs(yaw_error) > 0.80:
            # FIXED: Removed the incorrect negative sign from yaw_error
            turn = max(-0.60, min(0.60, yaw_error * 1.5))
            await self._client.move(0.0, 0.0, turn, 0.40)
            return f"pivoting toward {waypoint.name} (error: {yaw_error:.2f} rad)"

        # If we are roughly facing the target hemisphere, walk forward AND steer at the same time!
        alignment_factor = math.cos(yaw_error)
        vx = 0.30 * max(0.25, alignment_factor) 
        
        # FIXED: Removed the incorrect negative sign from yaw_error
        vyaw = max(-0.60, min(0.60, yaw_error * 1.8))
        
        # Stream the combined linear + angular velocity vector
        step_duration = min(0.50, max(0.30, distance * 0.25))
        await self._client.move(vx, 0.0, vyaw, step_duration)
        return f"driving toward {waypoint.name} (dist: {distance:.2f}m, error: {yaw_error:.2f} rad)"
        
    async def scan(self) -> None:
        """Perform a small visual scan without committing to travel."""
        await self._client.move(0.0, 0.0, 0.45, 0.35)
        await asyncio.sleep(0.05)
        await self._client.move(0.0, 0.0, -0.45, 0.35)

    async def stop(self) -> None:
        await self._client.stop()
