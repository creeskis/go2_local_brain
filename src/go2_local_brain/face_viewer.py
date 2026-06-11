"""Lightweight browser face viewer for Go2 video.

This module intentionally avoids the full autonomy GUI stack: no Ollama, no
workflows, no LiDAR, no robot motion controls. It only receives WebRTC video,
labels enrolled faces, and serves a tiny browser UI plus JSON status.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from .autonomy.face_id import FaceDatabase, FaceIdentifier, build_face_embedder
from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient

log = logging.getLogger(__name__)

_JPEG_QUALITY = 75


@dataclass
class FaceViewerState:
    latest_jpeg: bytes | None = None
    latest_video_ts: float = 0.0
    video_frames: int = 0
    decode_errors: int = 0
    face_errors: int = 0
    status: str = "starting"
    faces: list[dict[str, Any]] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


class FaceViewer:
    def __init__(
        self,
        *,
        robot_ip: str,
        host: str,
        port: int,
        backend: str,
        every: int,
        db_path: str | None,
    ) -> None:
        self._robot_ip = robot_ip
        self._host = host
        self._port = port
        self._backend = backend
        self._every = max(1, every)
        self._db_path = db_path
        self._state = FaceViewerState()
        self._state_changed = asyncio.Condition()
        self._client: Go2WebRTCClient | None = None
        self._identifier: FaceIdentifier | None = None

    async def run(self) -> None:
        self._load_faces()
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/video.mjpg", self._video_stream)
        app.router.add_get("/faces.json", self._faces_json)
        app.router.add_get("/status.json", self._faces_json)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("face viewer listening on http://%s:%s", self._host, self._port)
        try:
            await self._connect_robot()
            while True:
                await asyncio.sleep(3600)
        finally:
            if self._client is not None:
                await self._client.close()
            await runner.cleanup()

    def _load_faces(self) -> None:
        db = FaceDatabase.load_or_empty(self._db_path)
        embedder = build_face_embedder(self._backend)
        self._identifier = FaceIdentifier(embedder, db)
        self._state.labels = db.labels()
        log.info("loaded face labels: %s", ", ".join(self._state.labels) or "(none)")

    async def _connect_robot(self) -> None:
        cfg = load_config()
        self._state.status = "connecting"
        self._client = Go2WebRTCClient(
            Go2Config(
                ip=self._robot_ip,
                aes_128_key=cfg.go2_aes_128_key,
                webrtc_method=cfg.go2_webrtc_method,
                serial_number=cfg.go2_serial_number,
                remote_username=cfg.go2_remote_username,
                remote_password=cfg.go2_remote_password,
                remote_region=cfg.go2_remote_region,
                remote_device_type=cfg.go2_remote_device_type,
                force_motion_mode=cfg.force_motion_mode,
            )
        )
        await self._client.connect()
        conn = getattr(self._client, "_conn", None)
        video = getattr(conn, "video", None)
        if video is None:
            raise RuntimeError("WebRTC video interface not found")
        self._state.status = "connected"
        video.switchVideoChannel(True)
        video.add_track_callback(self._recv_video_track)

    async def _recv_video_track(self, track: Any) -> None:
        while True:
            try:
                frame = await track.recv()
                image = frame.to_image().convert("RGB")
                jpeg = _jpeg_from_image(image)
            except Exception as exc:  # noqa: BLE001
                self._state.decode_errors += 1
                if self._state.decode_errors <= 3:
                    log.warning("video decode failed: %s", exc)
                continue

            self._state.video_frames += 1
            if self._state.video_frames % self._every == 0:
                try:
                    self._state.faces = _identify_faces(image, self._identifier)
                except Exception as exc:  # noqa: BLE001
                    self._state.face_errors += 1
                    if self._state.face_errors <= 3:
                        log.warning("face identification failed: %s", exc)

            async with self._state_changed:
                self._state.latest_jpeg = jpeg
                self._state.latest_video_ts = time.time()
                self._state_changed.notify_all()

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _faces_json(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "robot_ip": self._robot_ip,
                "status": self._state.status,
                "video_frames": self._state.video_frames,
                "decode_errors": self._state.decode_errors,
                "face_errors": self._state.face_errors,
                "labels": self._state.labels,
                "faces": self._state.faces,
                "video_age_s": None
                if not self._state.latest_video_ts
                else time.time() - self._state.latest_video_ts,
            }
        )

    async def _video_stream(self, _request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"},
        )
        await response.prepare(_request)
        last_sent_ts = 0.0
        while True:
            async with self._state_changed:
                await self._state_changed.wait_for(
                    lambda: self._state.latest_jpeg is not None
                    and self._state.latest_video_ts != last_sent_ts
                )
                jpeg = self._state.latest_jpeg
                last_sent_ts = self._state.latest_video_ts
            if jpeg is None:
                continue
            await response.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
            await response.write(jpeg)
            await response.write(b"\r\n")


def _jpeg_from_image(image: Any) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return buf.getvalue()


def _face_boxes(image: Any) -> list[tuple[int, int, int, int]]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for live face boxes") from exc

    arr = np.asarray(image)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48))
    return [(int(x), int(y), int(x + w), int(y + h)) for x, y, w, h in faces]


def _identify_faces(image: Any, identifier: FaceIdentifier | None) -> list[dict[str, Any]]:
    width, height = image.size
    boxes = _face_boxes(image)
    identified = identifier.identify_faces(image, boxes) if identifier is not None else []
    faces: list[dict[str, Any]] = []
    for face in identified:
        faces.append(
            {
                "label": face.label,
                "score": face.score,
                "x": face.x,
                "y": face.y,
                "width": face.width,
                "height": face.height,
                "box": {
                    "left": max(0.0, (face.x - face.width / 2) / width),
                    "top": max(0.0, (face.y - face.height / 2) / height),
                    "width": min(1.0, face.width / width),
                    "height": min(1.0, face.height / height),
                },
            }
        )
    return faces


def _parse_args() -> argparse.Namespace:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="Serve lightweight Go2 face video to a browser")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8776)
    parser.add_argument("--robot-ip", default=cfg.go2_ip)
    parser.add_argument("--backend", choices=["insightface", "face_recognition"], default="insightface")
    parser.add_argument("--db", default=None)
    parser.add_argument("--every", type=int, default=5, help="Run face ID every N video frames")
    return parser.parse_args()


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    viewer = FaceViewer(
        robot_ip=args.robot_ip,
        host=args.host,
        port=args.port,
        backend=args.backend,
        every=args.every,
        db_path=args.db,
    )
    await viewer.run()


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Go2 Face Viewer</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; background: #080808; color: #f4f4f4; font-family: system-ui, Segoe UI, sans-serif; }
    header { height: 44px; display: flex; align-items: center; justify-content: space-between; padding: 0 14px; background: #181818; border-bottom: 1px solid #333; }
    main { height: calc(100vh - 44px); display: grid; grid-template-columns: 1fr 320px; }
    #stage { position: relative; display: flex; align-items: center; justify-content: center; overflow: hidden; background: #000; }
    #video { max-width: 100%; max-height: 100%; object-fit: contain; }
    #overlay { position: absolute; inset: 0; pointer-events: none; }
    .box { position: absolute; border: 3px solid #64f0a8; box-shadow: 0 0 12px rgba(100,240,168,.35); }
    .tag { position: absolute; left: -3px; top: -28px; background: #64f0a8; color: #04110a; padding: 3px 7px; font-weight: 700; font-size: 13px; }
    aside { border-left: 1px solid #333; padding: 14px; background: #101010; overflow: auto; }
    pre { white-space: pre-wrap; word-break: break-word; color: #cfcfcf; }
    @media (max-width: 850px) { main { grid-template-columns: 1fr; grid-template-rows: 1fr 220px; } aside { border-left: 0; border-top: 1px solid #333; } }
  </style>
</head>
<body>
  <header><strong>Go2 Face Viewer</strong><span id="status">starting</span></header>
  <main>
    <section id="stage">
      <img id="video" src="/video.mjpg" alt="Go2 video">
      <div id="overlay"></div>
    </section>
    <aside>
      <h3>Face Data</h3>
      <pre id="data">{}</pre>
    </aside>
  </main>
  <script>
    const video = document.getElementById("video");
    const overlay = document.getElementById("overlay");
    const data = document.getElementById("data");
    const status = document.getElementById("status");

    function render(payload) {
      status.textContent = `${payload.status} frames=${payload.video_frames} faces=${payload.faces.length}`;
      data.textContent = JSON.stringify(payload, null, 2);
      overlay.innerHTML = "";
      const stage = document.getElementById("stage").getBoundingClientRect();
      const img = video.getBoundingClientRect();
      for (const face of payload.faces) {
        const b = face.box;
        if (!b) continue;
        const el = document.createElement("div");
        el.className = "box";
        el.style.left = `${img.left - stage.left + b.left * img.width}px`;
        el.style.top = `${img.top - stage.top + b.top * img.height}px`;
        el.style.width = `${b.width * img.width}px`;
        el.style.height = `${b.height * img.height}px`;
        const tag = document.createElement("div");
        tag.className = "tag";
        tag.textContent = `${face.label} ${face.score.toFixed(2)}`;
        el.appendChild(tag);
        overlay.appendChild(el);
      }
    }

    async function poll() {
      try {
        const res = await fetch("/faces.json", {cache: "no-store"});
        render(await res.json());
      } catch (err) {
        status.textContent = "viewer disconnected";
      }
      setTimeout(poll, 500);
    }
    poll();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
