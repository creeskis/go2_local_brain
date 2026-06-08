"""Record repeated patrol runs and average successful paths."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .local_map import Pose2D


@dataclass(frozen=True)
class PathSample:
    timestamp: float
    x: float
    y: float
    yaw: float
    lidar_front_m: float | None = None

    def to_dict(self) -> dict[str, float | None]:
        return {
            "timestamp": round(self.timestamp, 3),
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "yaw": round(self.yaw, 4),
            "lidar_front_m": round(self.lidar_front_m, 3) if self.lidar_front_m is not None else None,
        }


@dataclass
class RecordedRun:
    name: str
    started_ts: float
    samples: list[PathSample] = field(default_factory=list)
    stopped_ts: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "started_ts": self.started_ts,
            "stopped_ts": self.stopped_ts,
            "sample_count": len(self.samples),
            "samples": [sample.to_dict() for sample in self.samples],
        }


class PathRunRecorder:
    """Small in-memory recorder for repeated route runs."""

    def __init__(self, *, min_step_m: float = 0.08, min_period_s: float = 0.25) -> None:
        self._min_step_m = min_step_m
        self._min_period_s = min_period_s
        self._runs: list[RecordedRun] = []
        self._active: RecordedRun | None = None

    @property
    def runs(self) -> list[RecordedRun]:
        return list(self._runs)

    @property
    def active(self) -> bool:
        return self._active is not None

    def start(self, name: str | None = None, *, now: float | None = None) -> RecordedRun:
        now = time.time() if now is None else now
        if self._active is not None:
            self.stop(now=now)
        run = RecordedRun(name=name or f"run-{len(self._runs) + 1}", started_ts=now)
        self._active = run
        return run

    def stop(self, *, now: float | None = None) -> RecordedRun | None:
        now = time.time() if now is None else now
        run = self._active
        if run is None:
            return None
        run.stopped_ts = now
        self._runs.append(run)
        self._active = None
        return run

    def add_pose(self, pose: Pose2D | None, *, lidar_front_m: float | None = None, now: float | None = None) -> None:
        if self._active is None or pose is None:
            return
        now = time.time() if now is None else now
        sample = PathSample(now, pose.x, pose.y, pose.yaw, lidar_front_m)
        if not self._active.samples:
            self._active.samples.append(sample)
            return
        last = self._active.samples[-1]
        moved = ((sample.x - last.x) ** 2 + (sample.y - last.y) ** 2) ** 0.5
        if moved < self._min_step_m and sample.timestamp - last.timestamp < self._min_period_s:
            return
        self._active.samples.append(sample)

    def average_path(self, *, points: int = 24) -> list[dict[str, float]]:
        usable = [run.samples for run in self._runs if len(run.samples) >= 2]
        if not usable:
            return []
        path: list[dict[str, float]] = []
        for i in range(points):
            t = i / max(1, points - 1)
            xs: list[float] = []
            ys: list[float] = []
            yaws: list[float] = []
            for samples in usable:
                sample = samples[min(len(samples) - 1, round(t * (len(samples) - 1)))]
                xs.append(sample.x)
                ys.append(sample.y)
                yaws.append(sample.yaw)
            path.append(
                {
                    "x": round(sum(xs) / len(xs), 3),
                    "y": round(sum(ys) / len(ys), 3),
                    "yaw": round(sum(yaws) / len(yaws), 4),
                }
            )
        return path

    def save(self, root: str | Path, *, name: str = "learned-runs") -> Path:
        path = Path(root) / "runs" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "run_count": len(self._runs),
            "runs": [run.to_dict() for run in self._runs],
            "average_path": self.average_path(),
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "run_count": len(self._runs),
            "active_samples": len(self._active.samples) if self._active is not None else 0,
            "last_run_samples": len(self._runs[-1].samples) if self._runs else 0,
            "average_points": len(self.average_path()),
        }
