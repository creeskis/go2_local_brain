"""Origin-locked local pose and trail state for browser mapping."""

from __future__ import annotations

import math
import time
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
    anchor_map_pose: Pose2D = field(default_factory=lambda: Pose2D(0.0, 0.0, 0.0))
    pose: Pose2D = field(default_factory=lambda: Pose2D(0.0, 0.0, 0.0))
    valid: bool = False
    source: str = "waiting"
    samples: int = 0
    last_update_ts: float = 0.0
    trail: list[Pose2D] = field(default_factory=list)

    def reset(self) -> None:
        self.origin_raw_pose = None
        self.anchor_map_pose = Pose2D(0.0, 0.0, 0.0)
        self.pose = Pose2D(0.0, 0.0, 0.0)
        self.valid = False
        self.source = "reset"
        self.samples = 0
        self.last_update_ts = 0.0
        self.trail.clear()

    def update_from_sport_state(self, sport_state: Any, *, now: float | None = None) -> Pose2D | None:
        now = time.time() if now is None else now
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
        delta_x = dx * cos_yaw + dy * sin_yaw
        delta_y = -dx * sin_yaw + dy * cos_yaw
        anchor = self.anchor_map_pose
        local_x = anchor.x + delta_x * math.cos(anchor.yaw) - delta_y * math.sin(anchor.yaw)
        local_y = anchor.y + delta_x * math.sin(anchor.yaw) + delta_y * math.cos(anchor.yaw)
        local_yaw = normalize_radians(anchor.yaw + raw_pose.yaw - origin.yaw)

        self.pose = Pose2D(local_x, local_y, local_yaw)
        self.valid = True
        self.source = "sport_state"
        self.samples += 1
        self.last_update_ts = now
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
            "anchor": self.anchor_map_pose.to_dict(),
            "last_update_ts": self.last_update_ts,
            "age_s": self.age_s(),
            "fresh": self.is_fresh(),
            "pose": self.current_pose_dict(),
            "trail": [pose.to_dict() for pose in self.trail],
        }

    def age_s(self, *, now: float | None = None) -> float | None:
        if self.last_update_ts <= 0:
            return None
        now = time.time() if now is None else now
        return round(now - self.last_update_ts, 3)

    def is_fresh(self, max_age_s: float = 1.0, *, now: float | None = None) -> bool:
        age = self.age_s(now=now)
        return age is not None and age <= max_age_s

    def lock_to_map_pose(self, map_pose: Pose2D, sport_state: Any, *, now: float | None = None) -> bool:
        raw_pose = raw_pose_from_sport_state(sport_state)
        if raw_pose is None:
            return False
        now = time.time() if now is None else now
        self.origin_raw_pose = raw_pose
        self.anchor_map_pose = map_pose
        self.pose = map_pose
        self.valid = True
        self.source = "manual_map_lock"
        self.samples += 1
        self.last_update_ts = now
        self.trail.clear()
        self.trail.append(map_pose)
        return True

    def _append_trail(self, pose: Pose2D) -> None:
        if not self.trail:
            self.trail.append(pose)
            return
        last = self.trail[-1]
        if math.hypot(pose.x - last.x, pose.y - last.y) < self.min_trail_step_m:
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
