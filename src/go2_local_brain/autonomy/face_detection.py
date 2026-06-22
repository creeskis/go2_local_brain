"""Pluggable face detectors for the local and simulated cockpits."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol


FaceBox = tuple[int, int, int, int]


class FaceDetector(Protocol):
    name: str

    def detect(self, image_rgb: Any) -> list[FaceBox]: ...


class HaarFaceDetector:
    name = "haar"

    def __init__(self, *, max_width: int = 360) -> None:
        self._max_width = max(160, int(max_width))
        self._cascade: Any = None

    def detect(self, image_rgb: Any) -> list[FaceBox]:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except ImportError as exc:
            raise RuntimeError("opencv-python is required for Haar face detection") from exc

        source = image_rgb.convert("RGB")
        width, height = max(1, source.width), max(1, source.height)
        scale = min(1.0, self._max_width / width)
        if scale < 1.0:
            source = source.resize((int(width * scale), int(height * scale)))
        gray = cv2.cvtColor(np.asarray(source), cv2.COLOR_RGB2GRAY)
        if self._cascade is None:
            self._cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
            if self._cascade.empty():
                raise RuntimeError("OpenCV face cascade is empty")
        faces = self._cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48)
        )
        return [
            (int(x / scale), int(y / scale), int((x + w) / scale), int((y + h) / scale))
            for x, y, w, h in faces
        ]


class YoloFaceDetector:
    """Ultralytics YOLO face detector with lazy model loading."""

    name = "yolo"

    def __init__(
        self,
        model: str | Path,
        *,
        confidence: float = 0.45,
        image_size: int = 640,
        device: str = "cpu",
    ) -> None:
        self._model_path = str(Path(model).expanduser())
        self._confidence = min(1.0, max(0.01, float(confidence)))
        self._image_size = max(160, int(image_size))
        self._device = device
        self._model: Any = None

    def _ensure(self) -> None:
        if self._model is not None:
            return
        if not Path(self._model_path).is_file():
            raise RuntimeError(f"YOLO face model not found: {self._model_path}")
        try:
            from ultralytics import YOLO  # type: ignore
        except ImportError as exc:
            raise RuntimeError("ultralytics is required for YOLO face detection") from exc
        self._model = YOLO(self._model_path)

    def detect(self, image_rgb: Any) -> list[FaceBox]:
        self._ensure()
        results = self._model.predict(
            source=image_rgb,
            conf=self._confidence,
            imgsz=self._image_size,
            device=self._device,
            verbose=False,
        )
        boxes: list[FaceBox] = []
        if not results:
            return boxes
        raw = getattr(results[0], "boxes", None)
        xyxy = getattr(raw, "xyxy", None)
        if xyxy is None:
            return boxes
        for values in xyxy.cpu().tolist():
            if len(values) >= 4:
                boxes.append(tuple(int(round(v)) for v in values[:4]))  # type: ignore[arg-type]
        return sorted(
            boxes,
            key=lambda box: max(0, box[2] - box[0]) * max(0, box[3] - box[1]),
            reverse=True,
        )


def build_face_detector(name: str | None = None) -> FaceDetector:
    key = (name or os.getenv("GO2_FACE_DETECTOR", "haar")).strip().lower()
    if key in {"haar", "opencv", "cascade"}:
        return HaarFaceDetector(max_width=int(os.getenv("GO2_FACE_DETECT_MAX_WIDTH", "360")))
    if key in {"yolo", "yolov8"}:
        model = os.getenv("GO2_FACE_YOLO_MODEL", "").strip()
        if not model:
            raise RuntimeError("GO2_FACE_YOLO_MODEL must point to a YOLO face model")
        return YoloFaceDetector(
            model,
            confidence=float(os.getenv("GO2_FACE_YOLO_CONFIDENCE", "0.45")),
            image_size=int(os.getenv("GO2_FACE_YOLO_IMAGE_SIZE", "640")),
            device=os.getenv("GO2_FACE_YOLO_DEVICE", "cpu"),
        )
    raise ValueError(f"unknown face detector backend: {key!r}")
