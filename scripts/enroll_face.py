"""Enroll a face into the recognition database.

Two sources:

  # From an image file:
  python scripts/enroll_face.py --label cooper --image cooper.jpg

  # From the Go2's live camera (grabs N frames, enrolls the largest face):
  python scripts/enroll_face.py --label cooper --camera --shots 5

The face database lives at ~/.config/go2_local_brain/faces.json (override
with $GO2_FACE_DB). Re-running with the same label adds more samples, which
improves matching robustness.

Backends (pick with --backend):
  insightface  - ONNX ArcFace, 512-d. Best on Jetson with GPU. (default)
  face_recognition - dlib, 128-d. Simpler CPU path.

Install one of:
  pip install insightface onnxruntime          # or onnxruntime-gpu on Jetson
  pip install face_recognition                  # needs cmake + dlib
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from go2_local_brain.autonomy.face_id import (
    FaceDatabase,
    FaceIdentifier,
    build_face_embedder,
)


def _largest_face_box(image_rgb) -> tuple[int, int, int, int] | None:
    """Find the largest face via OpenCV Haar; returns (x1,y1,x2,y2)."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        print("opencv (cv2) + numpy required for face boxing; pip install opencv-python", file=sys.stderr)
        return None
    arr = np.asarray(image_rgb)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return (int(x), int(y), int(x + w), int(y + h))


def _image_from_path(path: str):
    from PIL import Image  # type: ignore

    return Image.open(path).convert("RGB")


def _frames_from_camera(robot_ip: str, shots: int):
    """Grab a few JPEG frames from the Go2 over WebRTC. Yields PIL images."""
    import asyncio
    import io

    from PIL import Image  # type: ignore

    from go2_local_brain.driver.webrtc_client import Go2Config, Go2WebRTCClient

    images: list = []

    async def grab() -> None:
        client = Go2WebRTCClient(Go2Config(ip=robot_ip))
        await client.connect()
        conn = getattr(client, "_conn", None)
        video = getattr(conn, "video", None)
        if video is None:
            raise RuntimeError("no video interface on the connection")

        latest = {"jpeg": None}

        async def on_track(track):
            while len(images) < shots:
                frame = await track.recv()
                img = frame.to_image()
                buf = io.BytesIO()
                img.save(buf, format="JPEG")
                images.append(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))

        video.switchVideoChannel(True)
        video.add_track_callback(on_track)
        # Wait for enough frames.
        for _ in range(200):
            if len(images) >= shots:
                break
            await asyncio.sleep(0.1)
        await client.close()

    asyncio.run(grab())
    return images


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="Name to enroll (e.g. cooper)")
    parser.add_argument("--image", help="Path to an image file containing the face")
    parser.add_argument("--camera", action="store_true", help="Grab frames from the live Go2 camera")
    parser.add_argument("--shots", type=int, default=5, help="Frames to grab in camera mode")
    parser.add_argument("--robot-ip", default="192.168.123.121")
    parser.add_argument("--backend", choices=["insightface", "face_recognition"], default="insightface")
    parser.add_argument("--db", default=None, help="Face DB path (default: ~/.config/go2_local_brain/faces.json)")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else FaceDatabase.default_path()
    db = FaceDatabase.load_or_empty(db_path)
    embedder = build_face_embedder(args.backend)
    identifier = FaceIdentifier(embedder, db)

    if args.image:
        images = [_image_from_path(args.image)]
    elif args.camera:
        print(f"grabbing {args.shots} frames from {args.robot_ip} ...")
        images = _frames_from_camera(args.robot_ip, args.shots)
    else:
        print("provide --image PATH or --camera", file=sys.stderr)
        return 2

    enrolled = 0
    for image in images:
        box = _largest_face_box(image)
        if box is None:
            continue
        if identifier.enroll_from_image(args.label, image, box):
            enrolled += 1

    if enrolled == 0:
        print("no faces enrolled (no face detected, or embedder backend missing)", file=sys.stderr)
        return 1

    db.save(db_path)
    print(f"enrolled {enrolled} sample(s) for {args.label!r}; db now has labels: {db.labels()}")
    print(f"saved to {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
