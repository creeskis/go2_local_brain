"""Perception interfaces for autonomy mode.

The first implementation is intentionally conservative: it reports camera-frame
availability and leaves object detection pluggable for a later detector backend.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable


FrameSupplier = Callable[[], bytes | None]


@dataclass(frozen=True)
class Detection:
    """One compact visual detection."""

    label: str
    confidence: float
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None


@dataclass(frozen=True)
class Observation:
    """Compact world state sent to the autonomy planner/supervisor."""

    timestamp: float
    frame_available: bool
    detections: list[Detection] = field(default_factory=list)
    note: str = ""

    def summary(self) -> str:
        if not self.detections:
            return "camera=available; detections=none" if self.frame_available else "camera=missing; detections=none"
        labels = ", ".join(f"{d.label}:{d.confidence:.2f}" for d in self.detections[:6])
        return f"camera={'available' if self.frame_available else 'missing'}; detections={labels}"


class PerceptionProvider:
    """Interface for a detector that can summarize the latest camera frame."""

    async def observe(self) -> Observation:
        raise NotImplementedError


class NullPerceptionProvider(PerceptionProvider):
    """Frame-aware placeholder until a detector backend is installed."""

    def __init__(self, frame_supplier: FrameSupplier | None = None) -> None:
        self._frame_supplier = frame_supplier

    async def observe(self) -> Observation:
        frame = self._frame_supplier() if self._frame_supplier is not None else None
        return Observation(
            timestamp=time.time(),
            frame_available=frame is not None,
            detections=[],
            note="detector_backend=none",
        )


class CallbackPerceptionProvider(PerceptionProvider):
    """Adapter for future YOLO/AprilTag code without changing the supervisor."""

    def __init__(self, callback: Callable[[], Awaitable[Observation]]) -> None:
        self._callback = callback

    async def observe(self) -> Observation:
        return await self._callback()
