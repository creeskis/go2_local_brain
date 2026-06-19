"""Test Face ID from a host webcam without connecting to the robot.

Examples:
    python scripts/webcam_faceid_test.py --seconds 8
    python scripts/webcam_faceid_test.py --label Cooper --backend face_recognition
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
import sys
from typing import Any

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from go2_local_brain.autonomy.face_id import (  # noqa: E402
    FaceDatabase,
    FaceIdentifier,
    NullFaceEmbedder,
    UNKNOWN_LABEL,
    build_face_embedder,
)


def _load_cv2() -> Any:
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "OpenCV is required for webcam testing. Install with:\n"
            "  python -m pip install opencv-python\n"
            "or in WSL/venv:\n"
            "  pip install opencv-python"
        ) from exc
    return cv2


def _build_identifier(backend: str, db_path: Path) -> tuple[FaceIdentifier, str]:
    database = FaceDatabase.load_or_empty(db_path)
    try:
        embedder = build_face_embedder(backend)
        return FaceIdentifier(embedder, database), ""
    except Exception as exc:  # noqa: BLE001
        return FaceIdentifier(NullFaceEmbedder(), database), f"identity disabled: {exc}"


def _detect_faces(cv2: Any, frame_bgr: Any, *, max_width: int) -> list[tuple[int, int, int, int]]:
    height, width = frame_bgr.shape[:2]
    scale = 1.0
    small = frame_bgr
    if width > max_width:
        scale = max_width / width
        small = cv2.resize(frame_bgr, (int(width * scale), int(height * scale)))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        raise RuntimeError("OpenCV face cascade is empty")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48))
    return [
        (int(x / scale), int(y / scale), int((x + w) / scale), int((y + h) / scale))
        for x, y, w, h in faces
    ]


def _annotate(cv2: Any, frame_bgr: Any, faces: list[Any]) -> None:
    for face in faces:
        x1 = int(face.x - face.width / 2)
        y1 = int(face.y - face.height / 2)
        x2 = int(face.x + face.width / 2)
        y2 = int(face.y + face.height / 2)
        color = (55, 210, 75) if face.label != UNKNOWN_LABEL else (55, 190, 255)
        cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), color, 2)
        text = f"{face.label} {face.score:.2f}"
        cv2.putText(frame_bgr, text, (x1, max(18, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Host webcam Face ID test; no robot required")
    parser.add_argument("--camera", type=int, default=0, help="OpenCV camera index")
    parser.add_argument("--seconds", type=float, default=8.0, help="Capture duration")
    parser.add_argument("--backend", choices=["null", "face_recognition", "insightface"], default="null")
    parser.add_argument("--label", default="", help="Enroll the best visible face under this label")
    parser.add_argument("--db", default=str(FaceDatabase.default_path()))
    parser.add_argument("--max-width", type=int, default=360)
    parser.add_argument("--save", default="outputs/webcam_faceid_snapshot.jpg")
    parser.add_argument("--show", action="store_true", help="Show a local preview window")
    args = parser.parse_args()

    cv2 = _load_cv2()
    db_path = Path(args.db).expanduser()
    identifier, backend_error = _build_identifier(args.backend, db_path)

    api = cv2.CAP_DSHOW if sys.platform.startswith("win") else 0
    cap = cv2.VideoCapture(args.camera, api)
    if not cap.isOpened():
        raise SystemExit(f"could not open webcam index {args.camera}")

    best_frame = None
    best_faces: list[Any] = []
    enrolled = False
    frames = 0
    started = time.monotonic()
    try:
        while time.monotonic() - started < args.seconds:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            frames += 1
            boxes = _detect_faces(cv2, frame, max_width=args.max_width)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
            faces = identifier.identify_faces(image, boxes)
            if len(faces) >= len(best_faces):
                best_frame = frame.copy()
                best_faces = faces
            if args.label and faces and not enrolled:
                largest = max(
                    boxes,
                    key=lambda b: max(0, b[2] - b[0]) * max(0, b[3] - b[1]),
                )
                enrolled = identifier.enroll_from_image(args.label, image, largest)
                if enrolled:
                    identifier.database.save(db_path)
            if args.show:
                preview = frame.copy()
                _annotate(cv2, preview, faces)
                cv2.imshow("Go2 Face ID webcam test", preview)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        cap.release()
        if args.show:
            cv2.destroyAllWindows()

    if best_frame is not None:
        _annotate(cv2, best_frame, best_faces)
        save_path = (REPO_ROOT / args.save).resolve() if not Path(args.save).is_absolute() else Path(args.save)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(save_path), best_frame)
    else:
        save_path = None

    payload = {
        "frames": frames,
        "faces": [
            {"label": face.label, "score": face.score, "x": face.x, "y": face.y, "w": face.width, "h": face.height}
            for face in best_faces
        ],
        "backend": args.backend,
        "backend_error": backend_error,
        "db": str(db_path),
        "enrolled": enrolled,
        "snapshot": str(save_path) if save_path else None,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
