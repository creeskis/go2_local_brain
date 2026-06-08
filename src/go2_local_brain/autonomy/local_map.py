"""Origin-locked local pose and trail state for browser mapping."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Pose2D:
    """Robot pose in meters and radians."""

    x: float
    y: float
    yaw: float

    @property
    def yaw_deg(self) -> float:
        return math.degrees(self.yaw)

    def to_dict(self) -> dict[str, float]:
        return {"x": round(self.x, 3), "y": round(self.y, 3), "yaw": round(self.yaw_deg, 1)}


@dataclass
class LocalMapState:
    """Keeps a stable map origin and a compact pose trail."""

    max_trail_points: int = 500
    min_trail_step_m: float = 0.03
    origin_raw_pose: Pose2D | None = None
    pose: Pose2D = field(default_factory=lambda: Pose2D(0.0, 0.0, 0.0))
    valid: bool = False
    source: str = "waiting"
    samples: int = 0
    trail: list[Pose2D] = field(default_factory=list)

    def reset(self) -> None:
        self.origin_raw_pose = None
        self.pose = Pose2D(0.0, 0.0, 0.0)
        self.valid = False
        self.source = "reset"
        self.samples = 0
        self.trail.clear()

    def update_from_sport_state(self, sport_state: Any) -> Pose2D | None:
        raw_pose = raw_pose_from_sport_state(sport_state)
        if raw_pose is None:
            self.valid = False
            self.source = "sport_state_missing_pose"
            return None

        if self.origin_raw_pose is None:
            self.origin_raw_pose = raw_pose
            self.trail.clear()

        origin = self.origin_raw_pose
        dx = raw_pose.x - origin.x
        dy = raw_pose.y - origin.y
        cos_yaw = math.cos(origin.yaw)
        sin_yaw = math.sin(origin.yaw)
        local_x = dx * cos_yaw + dy * sin_yaw
        local_y = -dx * sin_yaw + dy * cos_yaw
        local_yaw = normalize_radians(raw_pose.yaw - origin.yaw)

        self.pose = Pose2D(local_x, local_y, local_yaw)
        self.valid = True
        self.source = "sport_state"
        self.samples += 1
        self._append_trail(self.pose)
        return self.pose

    def current_pose_dict(self) -> dict[str, float]:
        return self.pose.to_dict()

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "source": self.source,
            "samples": self.samples,
            "origin_locked": self.origin_raw_pose is not None,
            "pose": self.current_pose_dict(),
            "trail": [pose.to_dict() for pose in self.trail],
        }

    def _append_trail(self, pose: Pose2D) -> None:
        if not self.trail:
            self.trail.append(pose)
            return
        last = self.trail[-1]
        if math.hypot(pose.x - last.x, pose.y - last.y) < self.min_trail_step_m:
            self.trail[-1] = pose
            return
        self.trail.append(pose)
        if len(self.trail) > self.max_trail_points:
            del self.trail[: len(self.trail) - self.max_trail_points]


def raw_pose_from_sport_state(sport_state: Any) -> Pose2D | None:
    """Extract the raw robot pose reported by sport mode telemetry."""
    if not isinstance(sport_state, dict):
        return None

    position = sport_state.get("position")
    if not isinstance(position, list | tuple) or len(position) < 2:
        return None

    try:
        x = float(position[0])
        y = float(position[1])
    except (TypeError, ValueError):
        return None

    yaw = 0.0
    imu_state = sport_state.get("imu_state", {})
    if isinstance(imu_state, dict):
        rpy = imu_state.get("rpy", [])
        if isinstance(rpy, list | tuple) and len(rpy) >= 3:
            try:
                yaw = float(rpy[2])
            except (TypeError, ValueError):
                yaw = 0.0

    return Pose2D(x, y, yaw)


def normalize_radians(value: float) -> float:
    """Normalize an angle to [-pi, pi]."""
    return math.atan2(math.sin(value), math.cos(value))
