"""Perception interfaces for autonomy mode.

The first implementation is intentionally conservative: it reports camera-frame
availability and leaves object detection pluggable for a later detector backend.
"""

from __future__ import annotations

import time
import asyncio
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

    def is_human(self) -> bool:
        return self.label.lower() in {"person", "human"}

    def is_face(self) -> bool:
        return self.label.lower() in {"face", "human-face"}


@dataclass(frozen=True)
class Observation:
    """Compact world state sent to the autonomy planner/supervisor."""

    timestamp: float
    frame_available: bool
    detections: list[Detection] = field(default_factory=list)
    frame_width: int | None = None
    frame_height: int | None = None
    note: str = ""

    def summary(self) -> str:
        if not self.detections:
            return "camera=available; detections=none" if self.frame_available else "camera=missing; detections=none"
        labels = ", ".join(f"{d.label}:{d.confidence:.2f}" for d in self.detections[:6])
        return f"camera={'available' if self.frame_available else 'missing'}; detections={labels}"

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "frame_available": self.frame_available,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "detections": [detection_to_dict(d, self.frame_width, self.frame_height) for d in self.detections],
            "note": self.note,
            "summary": self.summary(),
        }


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
        detect_faces: bool = False,
        face_threshold: float = 0.0,
    ) -> None:
        self._frame_supplier = frame_supplier
        self._model_name = model_name
        self._threshold = threshold
        self._device = device
        self._detect_faces = detect_faces
        self._face_threshold = face_threshold
        self._model: object | None = None
        self._load_error = ""
        self._face_error = ""

    async def health(self) -> PerceptionHealth:
        if self._frame_supplier() is None:
            return PerceptionHealth(False, "yolo", "no camera frame available yet")
        model = self._load_model()
        if model is None:
            return PerceptionHealth(False, "yolo", self._load_error or "YOLO model unavailable")
        face_note = ""
        if self._detect_faces:
            face_note = "; face=opencv-optional"
        return PerceptionHealth(True, "yolo", f"model={self._model_name} threshold={self._threshold}{face_note}")

    async def observe(self) -> Observation:
        frame = self._frame_supplier()
        if frame is None:
            return Observation(timestamp=time.time(), frame_available=False, note="no frame")
        model = self._load_model()
        if model is None:
            return Observation(timestamp=time.time(), frame_available=True, note=self._load_error)
        return await asyncio.to_thread(self._predict_frame, frame, model)

    def _predict_frame(self, frame: bytes, model: object) -> Observation:
        image = _image_from_jpeg(frame)
        kwargs: dict[str, object] = {"verbose": False}
        if self._device:
            kwargs["device"] = self._device
        results = model.predict(image, **kwargs)  # type: ignore[attr-defined]
        width, height = getattr(image, "size", (None, None))
        detections = _detections_from_results(results, self._threshold)
        if self._detect_faces:
            detections.extend(_face_detections_from_image(image, self._face_threshold))
        note = "detector_backend=yolo"
        if self._face_error:
            note = f"{note}; face_error={self._face_error}"
        return Observation(
            timestamp=time.time(),
            frame_available=True,
            detections=detections,
            frame_width=int(width) if width is not None else None,
            frame_height=int(height) if height is not None else None,
            note=note,
        )

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


def _face_detections_from_image(image: object, threshold: float = 0.0) -> list[Detection]:
    """Optionally add OpenCV Haar face boxes when cv2 is installed."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return []

    try:
        frame = np.array(image)
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(cascade_path)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(32, 32))
    except Exception:
        return []

    confidence = max(0.01, threshold)
    detections: list[Detection] = []
    for x, y, width, height in faces:
        detections.append(
            Detection(
                label="face",
                confidence=confidence,
                x=float(x + width / 2),
                y=float(y + height / 2),
                width=float(width),
                height=float(height),
            )
        )
    return detections


def best_human_detection(observation: Observation) -> Detection | None:
    humans = [d for d in observation.detections if d.is_human()]
    if not humans:
        return None
    return max(humans, key=lambda d: d.confidence)


def detection_to_dict(detection: Detection, frame_width: int | None = None, frame_height: int | None = None) -> dict[str, object]:
    box = _normalized_box(detection, frame_width, frame_height)
    return {
        "label": detection.label,
        "confidence": detection.confidence,
        "x": detection.x,
        "y": detection.y,
        "width": detection.width,
        "height": detection.height,
        "kind": "human" if detection.is_human() else "face" if detection.is_face() else "object",
        "box": box,
    }


def _normalized_box(
    detection: Detection,
    frame_width: int | None,
    frame_height: int | None,
) -> dict[str, float] | None:
    if detection.x is None or detection.y is None or detection.width is None or detection.height is None:
        return None
    x = detection.x
    y = detection.y
    width = detection.width
    height = detection.height
    if x > 1.0 or y > 1.0 or width > 1.0 or height > 1.0:
        if not frame_width or not frame_height:
            return None
        x /= frame_width
        width /= frame_width
        y /= frame_height
        height /= frame_height
    left = max(0.0, min(1.0, x - width / 2))
    top = max(0.0, min(1.0, y - height / 2))
    return {
        "left": left,
        "top": top,
        "width": max(0.0, min(1.0 - left, width)),
        "height": max(0.0, min(1.0 - top, height)),
    }
