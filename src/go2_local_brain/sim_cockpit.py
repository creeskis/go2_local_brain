"""Host-only simulated cockpit for testing without the dog or Jetson."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
from pathlib import Path
import time
from typing import Any

from aiohttp import web
from PIL import Image, ImageDraw

from .autonomy.face_id import FaceDatabase, FaceIdentifier, NullFaceEmbedder, UNKNOWN_LABEL, build_face_embedder
from .local_cockpit import _INDEX_HTML, _face_boxes, _json_or_empty, _jpeg_from_image

log = logging.getLogger(__name__)

_FACE_ENABLED = os.getenv("GO2_FACE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
_FACE_INTERVAL_S = max(0.50, float(os.getenv("GO2_FACE_INTERVAL_S", "1.25")))
_DEFAULT_FACE_BACKEND = os.getenv("GO2_FACE_BACKEND", "null")


class SimGun:
    def __init__(self) -> None:
        self.active = False
        self.state = "idle"
        self.last_result = "sim gun idle"
        self.last_error = ""
        self.last_action_ts = 0.0
        self.log_tail: list[str] = ["sim relay ready; no hardware commands will run"]

    async def preconnect(self) -> str:
        return self._remember("ready", "sim gun relay ready")

    async def test(self) -> str:
        return self._remember("ready" if not self.active else "firing", "OK TEST sim")

    async def status(self) -> str:
        return self._remember("firing" if self.active else "ready", f"OK STATUS sim active={int(self.active)}")

    async def fire(self) -> str:
        self.active = True
        return self._remember("firing", "OK START sim trigger-held")

    async def stop(self) -> str:
        self.active = False
        return self._remember("ready", "OK STOP sim")

    async def close(self) -> None:
        self.active = False
        self.state = "idle"

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "state": self.state,
            "tunnel_alive": True,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "last_action_ts": self.last_action_ts,
            "log_file": "sim",
            "remote_log_file": "sim",
            "log_tail": self.log_tail[-12:],
        }

    def _remember(self, state: str, result: str) -> str:
        self.state = state
        self.last_result = result
        self.last_error = ""
        self.last_action_ts = time.time()
        self.log_tail.append(f"{time.strftime('%H:%M:%S')} {result}")
        return result


class SimCockpit:
    """Same browser surface as LocalCockpit, backed by local simulation."""

    def __init__(self, host: str, port: int, *, camera: int | None, fps: float) -> None:
        self._host = host
        self._port = port
        self._camera_index = camera
        self._frame_period_s = 1.0 / max(1.0, fps)
        self._state_changed = asyncio.Condition()
        self._latest_jpeg: bytes | None = None
        self._latest_video_ts = 0.0
        self._latest_image: Image.Image | None = None
        self._video_frames = 0
        self._status = "starting"
        self._last_result = "sim network booting"
        self._faces: list[dict[str, Any]] = []
        self._face_error = ""
        self._last_face_scan = 0.0
        self._face_task: asyncio.Task[list[dict[str, Any]]] | None = None
        self._face_backend = _DEFAULT_FACE_BACKEND
        self._face_db_path = Path(os.getenv("GO2_FACE_DB", str(FaceDatabase.default_path()))).expanduser()
        self._face_identifier: FaceIdentifier | None = None
        self._video_size = {"width": 640, "height": 480}
        self._gun = SimGun()
        self._battery_percent = 87.0
        self._pose = {"x": 0.0, "y": 0.0, "yaw": 0.0}
        self._last_move = {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}
        self._capture: Any = None
        self._cv2: Any = None
        self._frame_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        self._load_face_detector()
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get("/", self._index)
        app.router.add_get("/video.mjpg", self._video_stream)
        app.router.add_get("/status.json", self._status_json)
        app.router.add_post("/api/move", self._move_action)
        app.router.add_post("/api/stop", self._stop_action)
        app.router.add_post("/api/jump", self._jump_action)
        app.router.add_post("/api/sport", self._sport_action)
        app.router.add_post("/api/gun/preconnect", self._gun_preconnect)
        app.router.add_post("/api/gun/test", self._gun_test)
        app.router.add_post("/api/gun/status", self._gun_status)
        app.router.add_post("/api/gun/fire", self._gun_fire)
        app.router.add_post("/api/gun/stop", self._gun_stop)
        app.router.add_post("/api/face/enroll", self._face_enroll)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        self._status = "connected"
        self._last_result = "sim cockpit online"
        self._frame_task = asyncio.create_task(self._frame_loop(), name="go2-sim-frames")
        log.info("sim cockpit listening on http://%s:%s", self._host, self._port)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            if self._frame_task is not None:
                self._frame_task.cancel()
            if self._face_task is not None:
                self._face_task.cancel()
            if self._capture is not None:
                self._capture.release()
            await self._gun.close()
            await runner.cleanup()

    async def _index(self, _request: web.Request) -> web.Response:
        html = _INDEX_HTML.replace("Go2 Cockpit", "Go2 Sim Cockpit")
        html = html.replace("local video, drive, Face ID, optional trigger", "sim video, drive, Face ID, trigger test")
        return web.Response(text=html, content_type="text/html")

    async def _status_json(self, _request: web.Request) -> web.Response:
        return web.json_response(self._status_payload())

    async def _video_stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"},
        )
        await response.prepare(request)
        last_sent_ts = 0.0
        while True:
            async with self._state_changed:
                await self._state_changed.wait_for(
                    lambda: self._latest_jpeg is not None and self._latest_video_ts != last_sent_ts
                )
                jpeg = self._latest_jpeg
                last_sent_ts = self._latest_video_ts
            if jpeg is None:
                continue
            await response.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
            await response.write(jpeg)
            await response.write(b"\r\n")

    async def _frame_loop(self) -> None:
        self._open_camera()
        while True:
            started = time.monotonic()
            image = await asyncio.to_thread(self._next_frame)
            self._latest_image = image
            self._video_size = {"width": image.width, "height": image.height}
            now = time.monotonic()
            if _FACE_ENABLED and now - self._last_face_scan >= _FACE_INTERVAL_S and self._face_scan_available():
                self._last_face_scan = now
                self._start_face_scan(image)
            jpeg = _jpeg_from_image(image)
            async with self._state_changed:
                self._latest_jpeg = jpeg
                self._latest_video_ts = time.time()
                self._video_frames += 1
                self._state_changed.notify_all()
            await asyncio.sleep(max(0.0, self._frame_period_s - (time.monotonic() - started)))

    def _open_camera(self) -> None:
        if self._camera_index is None:
            return
        try:
            import cv2  # type: ignore
        except ImportError:
            self._face_error = "opencv missing; using generated sim video"
            return
        api = cv2.CAP_DSHOW if os.name == "nt" else 0
        cap = cv2.VideoCapture(self._camera_index, api)
        if not cap.isOpened():
            self._face_error = f"camera {self._camera_index} unavailable; using generated sim video"
            return
        self._cv2 = cv2
        self._capture = cap
        self._last_result = f"sim camera {self._camera_index} online"

    def _next_frame(self) -> Image.Image:
        if self._capture is not None and self._cv2 is not None:
            ok, frame = self._capture.read()
            if ok:
                rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
                return Image.fromarray(rgb).convert("RGB")
        return self._generated_frame()

    def _generated_frame(self) -> Image.Image:
        width, height = 960, 540
        t = time.monotonic()
        image = Image.new("RGB", (width, height), (7, 10, 13))
        draw = ImageDraw.Draw(image)
        for y in range(0, height, 36):
            shade = 20 + int(12 * math.sin(t + y * 0.03))
            draw.line((0, y, width, y), fill=(shade, shade + 4, shade + 8))
        cx = width / 2 + math.sin(t * 0.9) * 180
        cy = height / 2 + math.cos(t * 0.7) * 70
        draw.ellipse((cx - 58, cy - 58, cx + 58, cy + 58), outline=(241, 201, 74), width=4)
        draw.rectangle((cx - 34, cy - 10, cx + 34, cy + 54), outline=(241, 201, 74), width=3)
        draw.text((20, 18), "SIMULATED GO2 NETWORK - no robot commands leave this computer", fill=(210, 220, 226))
        draw.text((20, 44), f"pose x={self._pose['x']:.2f} y={self._pose['y']:.2f} yaw={self._pose['yaw']:.2f}", fill=(150, 190, 230))
        return image

    def _load_face_detector(self) -> None:
        database = FaceDatabase.load_or_empty(self._face_db_path)
        try:
            embedder = build_face_embedder(self._face_backend)
            self._face_identifier = FaceIdentifier(embedder, database)
        except Exception as exc:  # noqa: BLE001
            self._face_identifier = FaceIdentifier(NullFaceEmbedder(), database)
            self._face_error = f"recognition unavailable, detection only: {exc}"

    def _face_scan_available(self) -> bool:
        return self._face_task is None or self._face_task.done()

    def _start_face_scan(self, image: Image.Image) -> None:
        self._face_task = asyncio.create_task(self._identify_faces_off_loop(image), name="go2-sim-face-scan")
        self._face_task.add_done_callback(self._finish_face_scan)

    async def _identify_faces_off_loop(self, image: Image.Image) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._identify_faces, image)

    def _identify_faces(self, image: Image.Image) -> list[dict[str, Any]]:
        if self._face_identifier is None:
            return []
        try:
            boxes = _face_boxes(image)
            identified = self._face_identifier.identify_faces(image, boxes)
            width = max(1, image.width)
            height = max(1, image.height)
            return [
                {
                    "x": max(0.0, float(face.x - face.width / 2) / width),
                    "y": max(0.0, float(face.y - face.height / 2) / height),
                    "w": min(1.0, float(face.width) / width),
                    "h": min(1.0, float(face.height) / height),
                    "label": face.label,
                    "score": face.score,
                    "known": face.label != UNKNOWN_LABEL,
                }
                for face in identified[:8]
            ]
        except Exception as exc:  # noqa: BLE001
            self._face_error = str(exc)
            return []

    def _finish_face_scan(self, task: asyncio.Task[list[dict[str, Any]]]) -> None:
        try:
            self._faces = task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            self._face_error = str(exc)

    async def _move_action(self, request: web.Request) -> web.Response:
        payload = await _json_or_empty(request)
        vx = float(payload.get("vx", 0.0))
        vy = float(payload.get("vy", 0.0))
        vyaw = float(payload.get("vyaw", 0.0))
        duration = float(payload.get("duration_s", 0.2))
        self._pose["x"] += vx * duration
        self._pose["y"] += vy * duration
        self._pose["yaw"] += vyaw * duration
        self._last_move = {"vx": vx, "vy": vy, "vyaw": vyaw}
        self._last_result = f"sim move vx={vx:.2f} vy={vy:.2f} yaw={vyaw:.2f}"
        return web.json_response({"ok": True, "result": self._last_result})

    async def _stop_action(self, _request: web.Request) -> web.Response:
        self._last_move = {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}
        self._last_result = "sim stop"
        return web.json_response({"ok": True, "result": self._last_result})

    async def _jump_action(self, _request: web.Request) -> web.Response:
        self._last_result = "sim jump"
        return web.json_response({"ok": True, "result": self._last_result})

    async def _sport_action(self, request: web.Request) -> web.Response:
        payload = await _json_or_empty(request)
        name = str(payload.get("name", "")).strip() or "unknown"
        self._last_result = f"sim sport {name}"
        return web.json_response({"ok": True, "result": self._last_result})

    async def _gun_preconnect(self, _request: web.Request) -> web.Response:
        result = await self._gun.preconnect()
        return web.json_response({"ok": True, "result": result, "gun": self._gun.snapshot()})

    async def _gun_test(self, _request: web.Request) -> web.Response:
        result = await self._gun.test()
        return web.json_response({"ok": True, "result": result, "gun": self._gun.snapshot()})

    async def _gun_status(self, _request: web.Request) -> web.Response:
        result = await self._gun.status()
        return web.json_response({"ok": True, "result": result, "gun": self._gun.snapshot()})

    async def _gun_fire(self, _request: web.Request) -> web.Response:
        result = await self._gun.fire()
        return web.json_response({"ok": True, "result": result, "gun": self._gun.snapshot()})

    async def _gun_stop(self, _request: web.Request) -> web.Response:
        result = await self._gun.stop()
        return web.json_response({"ok": True, "result": result, "gun": self._gun.snapshot()})

    async def _face_enroll(self, request: web.Request) -> web.Response:
        payload = await _json_or_empty(request)
        label = str(payload.get("label", "")).strip()
        if not label:
            return web.json_response({"ok": False, "result": "face label is required"}, status=400)
        if self._face_identifier is None or self._latest_image is None or not self._faces:
            return web.json_response({"ok": False, "result": "no face is visible yet"}, status=400)
        face = self._faces[0]
        box = (
            int(face["x"] * self._latest_image.width),
            int(face["y"] * self._latest_image.height),
            int((face["x"] + face["w"]) * self._latest_image.width),
            int((face["y"] + face["h"]) * self._latest_image.height),
        )
        ok = self._face_identifier.enroll_from_image(label, self._latest_image, box)
        if not ok:
            return web.json_response({"ok": False, "result": "face embedding failed"}, status=400)
        path = self._face_identifier.database.save(self._face_db_path)
        self._last_result = f"sim enrolled {label}"
        return web.json_response({"ok": True, "result": f"enrolled {label} -> {path}"})

    def _status_payload(self) -> dict[str, Any]:
        self._battery_percent = max(42.0, self._battery_percent - 0.002)
        return {
            "status": self._status,
            "video_frames": self._video_frames,
            "last_result": self._last_result,
            "faces": self._faces,
            "video_size": self._video_size,
            "face_backend": self._face_backend,
            "face_error": self._face_error,
            "face_db": str(self._face_db_path),
            "sport_commands": ["BalanceStand", "RecoveryStand", "StandUp", "Sit", "Hello", "FrontJump"],
            "gun_active": self._gun.active,
            "gun": self._gun.snapshot(),
            "battery_percent": round(self._battery_percent, 1),
        }


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Host-only simulated Go2 cockpit")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8785)
    parser.add_argument("--camera", type=int, default=0, help="webcam index; use -1 for generated video")
    parser.add_argument("--fps", type=float, default=12.0)
    args = parser.parse_args()
    camera = None if args.camera < 0 else args.camera

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await SimCockpit(args.host, args.port, camera=camera, fps=args.fps).run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
