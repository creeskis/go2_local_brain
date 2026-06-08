"""Perception interfaces for autonomy mode.

The first implementation is intentionally conservative: it reports camera-frame
availability and leaves object detection pluggable for a later detector backend.
"""

from __future__ import annotations

import time
from io import BytesIO
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


@dataclass(frozen=True)
class PerceptionHealth:
    """Whether perception is trustworthy enough for autonomy."""

    ready: bool
    backend: str
    detail: str


class PerceptionProvider:
    """Interface for a detector that can summarize the latest camera frame."""

    async def observe(self) -> Observation:
        raise NotImplementedError

    async def health(self) -> PerceptionHealth:
        return PerceptionHealth(False, type(self).__name__, "health check not implemented")


class CameraOnlyPerceptionProvider(PerceptionProvider):
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

    async def health(self) -> PerceptionHealth:
        frame = self._frame_supplier() if self._frame_supplier is not None else None
        if frame is None:
            return PerceptionHealth(False, "camera-only", "no camera frame available yet")
        return PerceptionHealth(False, "camera-only", "camera frame available, but object detector is not configured")


NullPerceptionProvider = CameraOnlyPerceptionProvider


class CallbackPerceptionProvider(PerceptionProvider):
    """Adapter for future YOLO/AprilTag code without changing the supervisor."""

    def __init__(self, callback: Callable[[], Awaitable[Observation]]) -> None:
        self._callback = callback

    async def observe(self) -> Observation:
        return await self._callback()

    async def health(self) -> PerceptionHealth:
        return PerceptionHealth(True, "callback", "callback provider is configured")


class YoloPerceptionProvider(PerceptionProvider):
    """Optional Ultralytics YOLO detector over the latest MJPEG frame."""

    def __init__(
        self,
        frame_supplier: FrameSupplier,
        *,
        model_name: str = "yolov8n.pt",
        threshold: float = 0.55,
        device: str | None = None,
    ) -> None:
        self._frame_supplier = frame_supplier
        self._model_name = model_name
        self._threshold = threshold
        self._device = device
        self._model: object | None = None
        self._load_error = ""

    async def health(self) -> PerceptionHealth:
        if self._frame_supplier() is None:
            return PerceptionHealth(False, "yolo", "no camera frame available yet")
        model = self._load_model()
        if model is None:
            return PerceptionHealth(False, "yolo", self._load_error or "YOLO model unavailable")
        return PerceptionHealth(True, "yolo", f"model={self._model_name} threshold={self._threshold}")

    async def observe(self) -> Observation:
        frame = self._frame_supplier()
        if frame is None:
            return Observation(timestamp=time.time(), frame_available=False, note="no frame")
        model = self._load_model()
        if model is None:
            return Observation(timestamp=time.time(), frame_available=True, note=self._load_error)

        image = _image_from_jpeg(frame)
        kwargs: dict[str, object] = {"verbose": False}
        if self._device:
            kwargs["device"] = self._device
        results = model.predict(image, **kwargs)  # type: ignore[attr-defined]
        detections = _detections_from_results(results, self._threshold)
        return Observation(timestamp=time.time(), frame_available=True, detections=detections, note="detector_backend=yolo")

    def _load_model(self) -> object | None:
        if self._model is not None:
            return self._model
        try:
            from ultralytics import YOLO  # type: ignore

            self._model = YOLO(self._model_name)
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"install vision deps or choose camera detector: {exc}"
            return None
        return self._model


def _image_from_jpeg(frame: bytes) -> object:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("pillow is required for YOLO perception") from exc
    return Image.open(BytesIO(frame)).convert("RGB")


def _detections_from_results(results: object, threshold: float) -> list[Detection]:
    detections: list[Detection] = []
    for result in list(results if results is not None else []):
        names = getattr(result, "names", {}) or {}
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        for box in boxes:
            confidence = float(box.conf[0]) if getattr(box, "conf", None) is not None else 0.0
            if confidence < threshold:
                continue
            cls_id = int(box.cls[0]) if getattr(box, "cls", None) is not None else -1
            label = str(names.get(cls_id, cls_id))
            xywh = getattr(box, "xywh", None)
            x = y = width = height = None
            if xywh is not None:
                values = xywh[0].tolist()
                if len(values) >= 4:
                    x, y, width, height = (float(values[0]), float(values[1]), float(values[2]), float(values[3]))
            detections.append(Detection(label=label, confidence=confidence, x=x, y=y, width=width, height=height))
    return detections
