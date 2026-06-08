"""Small, interruptible navigation primitives with persistent global tracking."""

from __future__ import annotations

import asyncio
import math
from typing import Protocol

from .map import Waypoint


class RobotMover(Protocol):
    async def move(self, vx: float, vy: float = 0.0, vyaw: float = 0.0, duration_s: float = 0.0) -> None: ...
    async def stop(self) -> None: ...


class AutonomyNavigator:
    """Approximate waypoint movement with absolute frame transformation and yaw tracking."""

    def __init__(self, client: RobotMover) -> None:
        self._client = client
        self._initialized_global_frame = False
        self._initial_raw_x = 0.0
        self._initial_raw_y = 0.0
        self._initial_raw_yaw = 0.0

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

        # 1.5 INITIAL GLOBAL BASELINE LOCK
        # Lock in the zero-baseline references on the very first frame of the patrol session
        if not self._initialized_global_frame:
            self._initial_raw_x = current_x
            self._initial_raw_y = current_y
            self._initial_raw_yaw = current_yaw
            self._initialized_global_frame = True

        # Calculate true cumulative displacement coordinates since patrol initialization
        dx_raw = current_x - self._initial_raw_x
        dy_raw = current_y - self._initial_raw_y
        
        # Determine absolute distance gaps remaining on the global map grid
        dx_abs = waypoint.x - dx_raw
        dy_abs = waypoint.y - dy_raw

        # 3. Transform world coordinate offsets into the robot's current local frame
        cos_yaw = math.cos(current_yaw)
        sin_yaw = math.sin(current_yaw)
        dx_local = dx_abs * cos_yaw + dy_abs * sin_yaw
        dy_local = -dx_abs * sin_yaw + dy_abs * cos_yaw

        # 4. Compute true remaining linear distance
        distance = math.hypot(dx_local, dy_local)
        if distance < 0.25:
            # --- ARRIVED AT POSITION COORDINATES ---
            # Convert map waypoint target yaw from degrees (UI input) to radians
            target_yaw_rad = math.radians(waypoint.yaw)
            
            # Compute absolute current orientation normalized against the starting posture
            global_current_yaw = current_yaw - self._initial_raw_yaw
            heading_error = target_yaw_rad - global_current_yaw
            
            # Bound heading error within [-pi, pi] to ensure shortest-path rotation vectors
            heading_error = math.atan2(math.sin(heading_error), math.cos(heading_error))
            
            # Check if the dog's physical direction matches the waypoint's requested layout direction
            if abs(heading_error) > 0.15: # ~8.5 degree tolerance band
                # Note the wrong direction, execute a localized pivot maneuver to correct it
                turn = max(-0.45, min(0.45, heading_error * 1.5))
                await self._client.move(0.0, 0.0, turn, 0.35)
                return f"aligning orientation at {waypoint.name} (heading error: {math.degrees(heading_error):.1f}deg)"
            
            # Orientation pristine! Execute room sweep scan and pass tracking controls forward
            await self.scan()
            return f"scan at {waypoint.name}"

        # 5. Compute true tracking angle error to face the target node during transit
        yaw_error = math.atan2(dy_local, dx_local)

        # 5.5 LIVE OBSTACLE AVOIDANCE GUARD
        if len(range_obstacle) > 0 and 0.01 < range_obstacle[0] < 0.70:
            await self._client.move(-0.15, 0.0, 0.40, 0.40)
            return f"avoiding obstacle! object detected front: {range_obstacle[0]:.2f}m"

        # 6. BLENDED CONTROL LAW
        if abs(yaw_error) > 0.80:
            turn = max(-0.60, min(0.60, yaw_error * 1.5))
            await self._client.move(0.0, 0.0, turn, 0.40)
            return f"pivoting toward {waypoint.name} (error: {yaw_error:.2f} rad)"

        alignment_factor = math.cos(yaw_error)
        vx = 0.30 * max(0.25, alignment_factor) 
        vyaw = max(-0.60, min(0.60, yaw_error * 1.8))
        
        step_duration = min(0.50, max(0.30, distance * 0.25))
        await self._client.move(vx, 0.0, vyaw, step_duration)
        return f"driving toward {waypoint.name} (dist: {distance:.2f}m, error: {yaw_error:.2f} rad)"

    async def scan(self) -> None:
        """Perform a small visual scan without committing to travel."""
        await self._client.move(0.0, 0.0, 0.45, 0.35)
        await asyncio.sleep(0.05)
        await self._client.move(0.0, 0.0, -0.45, 0.35)

    async def stop(self) -> None:
        # Clear the global baseline anchor flag so the frame zeroes out cleanly on next activation
        self._initialized_global_frame = False
        await self._client.stop()
