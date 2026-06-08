"""Small, interruptible navigation primitives for local-map patrols."""

from __future__ import annotations

import asyncio
import math
from typing import Protocol

from .lidar_map import LidarObstacleField
from .local_map import LocalMapState, Pose2D, normalize_radians, raw_pose_from_sport_state
from .map import Waypoint


class RobotMover(Protocol):
    async def move(self, vx: float, vy: float = 0.0, vyaw: float = 0.0, duration_s: float = 0.0) -> None: ...
    async def stop(self) -> None: ...


class AutonomyNavigator:
    """Approximate waypoint movement using the same local frame as the map UI."""

    def __init__(
        self,
        client: RobotMover,
        local_map: LocalMapState | None = None,
        lidar_obstacles: LidarObstacleField | None = None,
    ) -> None:
        self._client = client
        self._local_map = local_map
        self._lidar_obstacles = lidar_obstacles
        self._fallback_origin: Pose2D | None = None

    async def move_toward(self, waypoint: Waypoint) -> str:
        """Take one smooth step toward a waypoint by blending forward and turning vectors."""
        sport_state = getattr(self._client, "_sport_state", None) or getattr(self._client, "sport_state", None)
        current_pose = self._current_pose(sport_state)
        current_x = 0.0
        current_y = 0.0
        current_yaw = 0.0
        range_obstacle = [0.0, 0.0, 0.0, 0.0]

        if current_pose is not None:
            current_x = current_pose.x
            current_y = current_pose.y
            current_yaw = current_pose.yaw

        if isinstance(sport_state, dict):
            range_obstacle = sport_state.get("range_obstacle", [0.0, 0.0, 0.0, 0.0])

        dx_abs = waypoint.x - current_x
        dy_abs = waypoint.y - current_y

        cos_yaw = math.cos(current_yaw)
        sin_yaw = math.sin(current_yaw)
        dx_local = dx_abs * cos_yaw + dy_abs * sin_yaw
        dy_local = -dx_abs * sin_yaw + dy_abs * cos_yaw

        distance = math.hypot(dx_local, dy_local)
        if distance < 0.25:
            target_yaw_rad = math.radians(waypoint.yaw)
            heading_error = normalize_radians(target_yaw_rad - current_yaw)

            if abs(heading_error) > 0.15:
                turn = max(-0.45, min(0.45, heading_error * 1.5))
                await self._client.move(0.0, 0.0, turn, 0.35)
                return f"aligning orientation at {waypoint.name} (heading error: {math.degrees(heading_error):.1f}deg)"

            await self.scan()
            return f"scan at {waypoint.name}"

        yaw_error = math.atan2(dy_local, dx_local)

        lidar_summary = self._lidar_obstacles.current_summary() if self._lidar_obstacles is not None else None
        if lidar_summary is not None and lidar_summary.fresh and lidar_summary.front_m is not None and lidar_summary.front_m < 0.70:
            turn = self._lidar_obstacles.recommended_avoidance_turn() if self._lidar_obstacles is not None else 0.40
            await self._client.move(-0.12, 0.0, turn, 0.35)
            return f"lidar avoid front obstacle {lidar_summary.front_m:.2f}m while heading to {waypoint.name}"

        if len(range_obstacle) > 0 and 0.01 < range_obstacle[0] < 0.70:
            await self._client.move(-0.15, 0.0, 0.40, 0.40)
            return f"avoiding obstacle! object detected front: {range_obstacle[0]:.2f}m"

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
        self._fallback_origin = None
        await self._client.stop()

    def _current_pose(self, sport_state: object) -> Pose2D | None:
        if self._local_map is not None:
            pose = self._local_map.update_from_sport_state(sport_state)
            if pose is not None:
                return pose

        raw_pose = raw_pose_from_sport_state(sport_state)
        if raw_pose is None:
            return Pose2D(0.0, 0.0, 0.0)
        if self._fallback_origin is None:
            self._fallback_origin = raw_pose
        origin = self._fallback_origin
        dx = raw_pose.x - origin.x
        dy = raw_pose.y - origin.y
        cos_yaw = math.cos(origin.yaw)
        sin_yaw = math.sin(origin.yaw)
        return Pose2D(
            dx * cos_yaw + dy * sin_yaw,
            -dx * sin_yaw + dy * cos_yaw,
            normalize_radians(raw_pose.yaw - origin.yaw),
        )
