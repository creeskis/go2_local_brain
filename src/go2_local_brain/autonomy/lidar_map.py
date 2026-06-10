"""LiDAR-derived local obstacle and occupancy helpers."""

from __future__ import annotations

import math
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .local_map import Pose2D


@dataclass(frozen=True)
class LidarObstacleSummary:
    """Robot-relative obstacle distances by sector."""

    point_count: int
    front_m: float | None
    left_m: float | None
    right_m: float | None
    rear_m: float | None
    fresh: bool
    age_s: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "point_count": self.point_count,
            "front_m": _round_optional(self.front_m),
            "left_m": _round_optional(self.left_m),
            "right_m": _round_optional(self.right_m),
            "rear_m": _round_optional(self.rear_m),
            "fresh": self.fresh,
            "age_s": _round_optional(self.age_s),
            "blocked_front": self.front_m is not None and self.front_m < 0.70,
        }


@dataclass
class LidarTransform:
    """Calibration transform applied before LiDAR is used for maps or avoidance."""

    rotate_deg: float = 0.0
    flip_x: bool = False
    flip_y: bool = False
    swap_xy: bool = False

    def apply(self, points: list[list[float]]) -> list[list[float]]:
        if not points:
            return []
        theta = math.radians(self.rotate_deg)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        out: list[list[float]] = []
        for x, y, z in _coerce_points(points):
            if self.swap_xy:
                x, y = y, x
            if self.flip_x:
                x = -x
            if self.flip_y:
                y = -y
            out.append([x * cos_t - y * sin_t, x * sin_t + y * cos_t, z])
        return out

    def to_dict(self) -> dict[str, object]:
        return {
            "rotate_deg": self.rotate_deg,
            "flip_x": self.flip_x,
            "flip_y": self.flip_y,
            "swap_xy": self.swap_xy,
        }

    @classmethod
    def from_values(
        cls,
        *,
        rotate_deg: float | str | None = None,
        flip_x: bool | str | None = None,
        flip_y: bool | str | None = None,
        swap_xy: bool | str | None = None,
    ) -> "LidarTransform":
        return cls(
            rotate_deg=float(rotate_deg or 0.0),
            flip_x=_truthy(flip_x),
            flip_y=_truthy(flip_y),
            swap_xy=_truthy(swap_xy),
        )


@dataclass
class LidarObstacleField:
    """Keeps the latest robot-relative LiDAR cloud and clearance sectors."""

    max_age_s: float = 0.75
    min_distance_m: float = 0.08
    max_distance_m: float = 4.0
    last_update_ts: float = 0.0
    robot_points: list[list[float]] = field(default_factory=list)
    summary: LidarObstacleSummary = field(
        default_factory=lambda: LidarObstacleSummary(0, None, None, None, None, False, None)
    )

    def update(self, robot_points: list[list[float]], *, now: float | None = None) -> LidarObstacleSummary:
        now = time.time() if now is None else now
        points = _valid_ground_points(robot_points, self.min_distance_m, self.max_distance_m)
        self.robot_points = points
        self.last_update_ts = now
        self.summary = self._summarize(now)
        return self.summary

    def current_summary(self, *, now: float | None = None) -> LidarObstacleSummary:
        now = time.time() if now is None else now
        if self.last_update_ts <= 0:
            return LidarObstacleSummary(0, None, None, None, None, False, None)
        age_s = now - self.last_update_ts
        fresh = age_s <= self.max_age_s
        return LidarObstacleSummary(
            self.summary.point_count,
            self.summary.front_m,
            self.summary.left_m,
            self.summary.right_m,
            self.summary.rear_m,
            fresh,
            age_s,
        )

    def recommended_avoidance_turn(self) -> float:
        summary = self.current_summary()
        left = summary.left_m if summary.left_m is not None else self.max_distance_m
        right = summary.right_m if summary.right_m is not None else self.max_distance_m
        return -0.45 if right >= left else 0.45

    def _summarize(self, now: float) -> LidarObstacleSummary:
        sectors: dict[str, list[float]] = {"front": [], "left": [], "right": [], "rear": []}
        for x, y, _z in self.robot_points:
            distance = math.hypot(x, y)
            angle = math.degrees(math.atan2(y, x))
            if abs(angle) <= 35:
                sectors["front"].append(distance)
            elif 35 < angle < 135:
                sectors["left"].append(distance)
            elif -135 < angle < -35:
                sectors["right"].append(distance)
            else:
                sectors["rear"].append(distance)
        return LidarObstacleSummary(
            point_count=len(self.robot_points),
            front_m=_min_or_none(sectors["front"]),
            left_m=_min_or_none(sectors["left"]),
            right_m=_min_or_none(sectors["right"]),
            rear_m=_min_or_none(sectors["rear"]),
            fresh=True,
            age_s=0.0 if self.last_update_ts else None,
        )


@dataclass
class LidarLocalMapper:
    """Accumulates LiDAR hits into a coarse local map-frame occupancy grid."""

    cell_size_m: float = 0.15
    max_cells: int = 6000
    cells: dict[tuple[int, int], int] = field(default_factory=dict)
    last_update_ts: float = 0.0

    def reset(self) -> None:
        self.cells.clear()
        self.last_update_ts = 0.0

    def add_scan(self, pose: Pose2D | None, robot_points: list[list[float]], *, now: float | None = None) -> None:
        if pose is None:
            return
        now = time.time() if now is None else now
        cos_yaw = math.cos(pose.yaw)
        sin_yaw = math.sin(pose.yaw)
        for x, y, _z in _valid_ground_points(robot_points, 0.08, 5.0):
            map_x = pose.x + x * cos_yaw - y * sin_yaw
            map_y = pose.y + x * sin_yaw + y * cos_yaw
            cell = (round(map_x / self.cell_size_m), round(map_y / self.cell_size_m))
            self.cells[cell] = min(255, self.cells.get(cell, 0) + 1)
        self.last_update_ts = now
        if len(self.cells) > self.max_cells:
            self._trim()

    def to_dict(self, *, max_cells: int = 1200) -> dict[str, object]:
        strongest = sorted(self.cells.items(), key=lambda item: item[1], reverse=True)[:max_cells]
        return {
            "cell_size_m": self.cell_size_m,
            "cell_count": len(self.cells),
            "last_update_ts": self.last_update_ts,
            "cells": [
                {"x": ix * self.cell_size_m, "y": iy * self.cell_size_m, "count": count}
                for (ix, iy), count in strongest
            ],
        }

    def save(self, root: str | Path, *, name: str = "lidar-occupancy") -> Path:
        path = Path(root) / "runs" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "saved_ts": time.time(), **self.to_dict(max_cells=self.max_cells)}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _trim(self) -> None:
        strongest = sorted(self.cells.items(), key=lambda item: item[1], reverse=True)[: self.max_cells]
        self.cells = dict(strongest)


def points_from_lidar_payload(payload: dict[str, Any] | None) -> list[list[float]]:
    if not isinstance(payload, dict):
        return []
    points = payload.get("robot_points") or payload.get("raw_points") or []
    if not isinstance(points, list):
        return []
    return _coerce_points(points)


def lidar_debug_payload(
    *,
    raw_messages: int,
    parsed_messages: int,
    parse_errors: int,
    latest_payload: dict[str, Any] | None,
    latest_ts: float,
    transform: LidarTransform | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return operator-facing LiDAR diagnostics that are safe to expose over HTTP."""
    now = time.time() if now is None else now
    latest = latest_payload or {}
    points = points_from_lidar_payload(latest)
    transformed = transform.apply(points) if transform is not None else points
    return {
        "raw_messages": raw_messages,
        "parsed_messages": parsed_messages,
        "parse_errors": parse_errors,
        "parse_error_rate": round(parse_errors / raw_messages, 3) if raw_messages else 0.0,
        "age_s": round(now - latest_ts, 3) if latest_ts else None,
        "point_count": len(points),
        "transformed_point_count": len(transformed),
        "source_point_count": latest.get("source_point_count", 0),
        "bounds": latest.get("bounds"),
        "robot_bounds": point_bounds(points),
        "transformed_bounds": point_bounds(transformed),
        "source_keys": sorted(latest.keys()),
        "transform": transform.to_dict() if transform is not None else LidarTransform().to_dict(),
        "sample_robot_points": points[:8],
        "sample_transformed_points": transformed[:8],
    }


def point_bounds(points: list[list[float]]) -> dict[str, float] | None:
    coerced = _coerce_points(points)
    if not coerced:
        return None
    xs = [point[0] for point in coerced]
    ys = [point[1] for point in coerced]
    zs = [point[2] for point in coerced]
    return {
        "min_x": round(min(xs), 3),
        "max_x": round(max(xs), 3),
        "min_y": round(min(ys), 3),
        "max_y": round(max(ys), 3),
        "min_z": round(min(zs), 3),
        "max_z": round(max(zs), 3),
    }


def _valid_ground_points(points: list[list[float]], min_distance_m: float, max_distance_m: float) -> list[list[float]]:
    out: list[list[float]] = []
    for x, y, z in points:
        distance = math.hypot(x, y)
        if min_distance_m <= distance <= max_distance_m and -0.45 <= z <= 1.25:
            out.append([x, y, z])
    return out


def _coerce_points(points: list[Any]) -> list[list[float]]:
    out: list[list[float]] = []
    for point in points:
        if not isinstance(point, list | tuple) or len(point) < 3:
            continue
        try:
            out.append([float(point[0]), float(point[1]), float(point[2])])
        except (TypeError, ValueError):
            continue
    return out


def _min_or_none(values: list[float]) -> float | None:
    return min(values) if values else None


def _round_optional(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _truthy(value: bool | str | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
