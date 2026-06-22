"""Unit tests for face detector selection and box handling."""

from __future__ import annotations

import unittest

from go2_local_brain.autonomy.face_detection import YoloFaceDetector, build_face_detector


class _Tensor:
    def __init__(self, values):
        self._values = values

    def cpu(self):
        return self

    def tolist(self):
        return self._values


class _Model:
    def predict(self, **_kwargs):
        boxes = type("Boxes", (), {"xyxy": _Tensor([[1, 1, 11, 11], [2, 2, 42, 42]])})()
        return [type("Result", (), {"boxes": boxes})()]


class FaceDetectorTests(unittest.TestCase):
    def test_yolo_returns_largest_face_first(self) -> None:
        detector = YoloFaceDetector("unused.pt")
        detector._model = _Model()
        self.assertEqual(detector.detect(object()), [(2, 2, 42, 42), (1, 1, 11, 11)])

    def test_unknown_detector_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_face_detector("not-a-detector")


if __name__ == "__main__":
    unittest.main()
