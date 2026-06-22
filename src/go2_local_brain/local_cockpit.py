"""Localhost-only WASD/video/face/gun cockpit."""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
from pathlib import Path
import time
from typing import Any

from aiohttp import web

from .config import load_config
from .autonomy.face_id import FaceDatabase, FaceIdentifier, NullFaceEmbedder, UNKNOWN_LABEL, build_face_embedder
from .autonomy.face_detection import FaceDetector, build_face_detector
from .autonomy.follow import HumanFollowController
from .autonomy.perception import Observation, PerceptionHealth, YoloPerceptionProvider, detection_to_dict
from .driver.webrtc_client import Go2Config, Go2WebRTCClient
from .gun_relay import GunRelay, gun_relay_config_from_env

log = logging.getLogger(__name__)

_MOVE_DURATION_S = 0.20
_FACE_ENABLED = os.getenv("GO2_FACE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
_FACE_INTERVAL_S = max(0.10, float(os.getenv("GO2_FACE_INTERVAL_S", "1.25")))
_FACE_DETECT_MAX_WIDTH = max(160, int(os.getenv("GO2_FACE_DETECT_MAX_WIDTH", "360")))
_FACE_MAX_RESULTS = max(2, int(os.getenv("GO2_FACE_MAX_RESULTS", "16")))
_DEFAULT_FACE_BACKEND = "face_recognition"
_JPEG_QUALITY = min(90, max(35, int(os.getenv("GO2_JPEG_QUALITY", "68"))))
_FACE_CASCADE: Any | None = None
_FOLLOW_ENABLED = os.getenv("GO2_FOLLOW_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
_FOLLOW_MODEL = os.getenv("GO2_FOLLOW_YOLO_MODEL", "yolov8n.pt")
_FOLLOW_THRESHOLD = float(os.getenv("GO2_FOLLOW_YOLO_THRESHOLD", "0.38"))
_FOLLOW_DEVICE = os.getenv("GO2_FOLLOW_YOLO_DEVICE", "").strip() or None
_FOLLOW_INTERVAL_S = max(0.10, float(os.getenv("GO2_FOLLOW_INTERVAL_S", "0.25")))


class LocalCockpit:
    """Small localhost cockpit: video, WASD, face boxes, and SSH gun control."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._client: Go2WebRTCClient | None = None
        self._gun = GunRelay(gun_relay_config_from_env())
        self._state_changed = asyncio.Condition()
        self._latest_jpeg: bytes | None = None
        self._latest_video_ts = 0.0
        self._video_frames = 0
        self._status = "starting"
        self._last_result = ""
        self._faces: list[dict[str, Any]] = []
        self._video_size = {"width": 0, "height": 0}
        self._face_error = ""
        self._last_face_scan = 0.0
        self._face_backend = os.getenv("GO2_FACE_BACKEND", _DEFAULT_FACE_BACKEND)
        self._face_identifier: FaceIdentifier | None = None
        self._face_detector: FaceDetector | None = None
        self._face_detector_name = os.getenv("GO2_FACE_DETECTOR", "haar")
        self._face_db_path = Path(os.getenv("GO2_FACE_DB", str(FaceDatabase.default_path()))).expanduser()
        self._latest_image: Any = None
        self._faces_image: Any = None
        self._face_task: asyncio.Task[tuple[list[dict[str, Any]], Any]] | None = None
        self._battery_percent: float | None = None
        self._available_sport_commands: list[str] = []
        self._perception: YoloPerceptionProvider | None = None
        self._perception_health = PerceptionHealth(False, "not-started", "waiting for video")
        self._latest_observation = Observation(timestamp=0.0, frame_available=False, note="waiting for video")
        self._perception_task: asyncio.Task[None] | None = None
        self._follow: HumanFollowController | None = None
        self._follow_task: asyncio.Task[None] | None = None
        self._follow_last_action = "idle"

    async def run(self) -> None:
        cfg = load_config()
        self._client = Go2WebRTCClient(
            Go2Config(
                ip=cfg.go2_ip,
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
        app.router.add_post("/api/follow/{action}", self._follow_action)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("local cockpit listening on http://%s:%s", self._host, self._port)

        try:
            await self._connect()
            while True:
                await asyncio.sleep(3600)
        finally:
            await self._gun.close()
            await self._shutdown()
            await runner.cleanup()

    async def _connect(self) -> None:
        assert self._client is not None
        self._status = "connecting"
        await self._client.connect()
        self._available_sport_commands = self._client.available_sport_commands()
        self._attach_video()
        if _FOLLOW_ENABLED:
            self._perception = YoloPerceptionProvider(
                lambda: self._latest_jpeg,
                model_name=_FOLLOW_MODEL,
                threshold=_FOLLOW_THRESHOLD,
                device=_FOLLOW_DEVICE,
            )
            self._follow = HumanFollowController(
                self._client,
                target_height=float(os.getenv("GO2_FOLLOW_TARGET_HEIGHT", "0.45")),
                max_forward=float(os.getenv("GO2_FOLLOW_MAX_FORWARD", "0.30")),
                max_turn=float(os.getenv("GO2_FOLLOW_MAX_TURN", "0.45")),
                duration_s=float(os.getenv("GO2_FOLLOW_MOVE_DURATION", "0.28")),
            )
            self._perception_task = asyncio.create_task(self._perception_loop(), name="go2-local-person-detection")
        self._status = "connected"

    async def _shutdown(self) -> None:
        await self._stop_follow()
        if self._perception_task is not None:
            self._perception_task.cancel()
            try:
                await self._perception_task
            except asyncio.CancelledError:
                pass
        if self._face_task is not None:
            self._face_task.cancel()
        if self._client is not None:
            await self._client.close()

    def _attach_video(self) -> None:
        assert self._client is not None
        conn = getattr(self._client, "_conn", None)
        video = getattr(conn, "video", None)
        if video is None:
            raise RuntimeError("WebRTC video interface not found")
        video.switchVideoChannel(True)
        video.add_track_callback(self._recv_video_track)

    async def _recv_video_track(self, track: Any) -> None:
        while True:
            frame = await track.recv()
            image = frame.to_image()
            self._latest_image = image.convert("RGB")
            self._video_size = {"width": int(getattr(image, "width", 0)), "height": int(getattr(image, "height", 0))}
            now = time.monotonic()
            if _FACE_ENABLED and now - self._last_face_scan >= _FACE_INTERVAL_S and self._face_scan_available():
                self._last_face_scan = now
                self._start_face_scan(self._latest_image)
            jpeg = _jpeg_from_image(image)
            async with self._state_changed:
                self._latest_jpeg = jpeg
                self._latest_video_ts = time.time()
                self._video_frames += 1
                self._state_changed.notify_all()

    def _load_face_detector(self) -> None:
        database = FaceDatabase.load_or_empty(self._face_db_path)
        try:
            self._face_detector = build_face_detector(self._face_detector_name)
        except Exception as exc:  # noqa: BLE001
            self._face_error = f"detector unavailable: {exc}"
        try:
            embedder = build_face_embedder(self._face_backend)
            self._face_identifier = FaceIdentifier(embedder, database)
        except Exception as exc:  # noqa: BLE001
            self._face_identifier = FaceIdentifier(NullFaceEmbedder(), database)
            self._face_error = f"recognition unavailable, detection only: {exc}"

    def _identify_faces(self, image: Any) -> list[dict[str, Any]]:
        if self._face_identifier is None:
            return []
        try:
            if self._face_detector is None:
                return []
            boxes = self._face_detector.detect(image)
            identified = self._face_identifier.identify_faces(image, boxes)
            width = max(1, int(getattr(image, "width", 1)))
            height = max(1, int(getattr(image, "height", 1)))
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
                for face in identified[:_FACE_MAX_RESULTS]
            ]
        except Exception as exc:  # noqa: BLE001
            self._face_error = str(exc)
            return []

    def _face_scan_available(self) -> bool:
        return self._face_task is None or self._face_task.done()

    def _start_face_scan(self, image: Any) -> None:
        self._face_task = asyncio.create_task(self._identify_faces_off_loop(image), name="go2-face-scan")
        self._face_task.add_done_callback(self._finish_face_scan)

    async def _identify_faces_off_loop(self, image: Any) -> tuple[list[dict[str, Any]], Any]:
        faces = await asyncio.to_thread(self._identify_faces, image)
        return faces, image

    def _finish_face_scan(self, task: asyncio.Task[tuple[list[dict[str, Any]], Any]]) -> None:
        try:
            self._faces, self._faces_image = task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            self._face_error = str(exc)

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

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

    async def _move_action(self, request: web.Request) -> web.Response:
        if self._client is None:
            return web.json_response({"ok": False, "result": "robot is not connected"}, status=503)
        payload = await _json_or_empty(request)
        await self._stop_follow(stop_robot=False)
        try:
            vx = float(payload.get("vx", 0.0))
            vy = float(payload.get("vy", 0.0))
            vyaw = float(payload.get("vyaw", 0.0))
            duration = float(payload.get("duration_s", _MOVE_DURATION_S))
            await self._client.move(vx, vy, vyaw, duration)
            result = f"move vx={vx:.2f} vy={vy:.2f} yaw={vyaw:.2f}"
        except Exception as exc:  # noqa: BLE001
            log.exception("move failed")
            await self._safe_stop()
            return web.json_response({"ok": False, "result": f"move failed: {exc}"}, status=400)
        self._last_result = result
        return web.json_response({"ok": True, "result": result})

    async def _face_enroll(self, request: web.Request) -> web.Response:
        payload = await _json_or_empty(request)
        label = str(payload.get("label", "")).strip()
        index = int(payload.get("index", 0) or 0)
        if not label:
            return web.json_response({"ok": False, "result": "face label is required"}, status=400)
        if self._face_identifier is None:
            return web.json_response({"ok": False, "result": f"face backend unavailable: {self._face_error}"}, status=400)
        if self._faces_image is None or not self._faces:
            return web.json_response({"ok": False, "result": "no face is visible yet"}, status=400)
        face = self._faces[min(max(index, 0), len(self._faces) - 1)]
        width = max(1, int(getattr(self._faces_image, "width", 1)))
        height = max(1, int(getattr(self._faces_image, "height", 1)))
        box = (
            int(face["x"] * width),
            int(face["y"] * height),
            int((face["x"] + face["w"]) * width),
            int((face["y"] + face["h"]) * height),
        )
        try:
            ok = self._face_identifier.enroll_from_image(label, self._faces_image, box)
            if not ok:
                return web.json_response({"ok": False, "result": "face embedding failed"}, status=400)
            path = self._face_identifier.database.save(self._face_db_path)
        except Exception as exc:  # noqa: BLE001
            log.exception("face enroll failed")
            return web.json_response({"ok": False, "result": f"face enroll failed: {exc}"}, status=400)
        self._last_result = f"enrolled face {label}"
        return web.json_response({"ok": True, "result": f"enrolled {label} -> {path}"})

    async def _stop_action(self, _request: web.Request) -> web.Response:
        await self._stop_follow(stop_robot=False)
        await self._safe_stop()
        self._last_result = "stop"
        return web.json_response({"ok": True, "result": "stop"})

    async def _jump_action(self, _request: web.Request) -> web.Response:
        if self._client is None:
            return web.json_response({"ok": False, "result": "robot is not connected"}, status=503)
        try:
            await self._stop_follow(stop_robot=False)
            await self._client.advanced_action("jump")
        except Exception as exc:  # noqa: BLE001
            log.exception("jump failed")
            await self._safe_stop()
            return web.json_response({"ok": False, "result": f"jump failed: {exc}"}, status=400)
        self._last_result = "jump"
        return web.json_response({"ok": True, "result": "jump"})

    async def _sport_action(self, request: web.Request) -> web.Response:
        if self._client is None:
            return web.json_response({"ok": False, "result": "robot is not connected"}, status=503)
        payload = await _json_or_empty(request)
        name = str(payload.get("name", "")).strip()
        parameter = payload.get("parameter")
        if not name:
            return web.json_response({"ok": False, "result": "sport command name is required"}, status=400)
        if parameter is not None and not isinstance(parameter, dict):
            return web.json_response({"ok": False, "result": "sport parameter must be an object"}, status=400)
        try:
            await self._stop_follow(stop_robot=False)
            await self._client.sport_command(name, parameter)
        except Exception as exc:  # noqa: BLE001
            log.exception("sport command failed: %s", name)
            return web.json_response({"ok": False, "result": f"sport command failed: {exc}"}, status=400)
        self._last_result = f"sport {name}"
        return web.json_response({"ok": True, "result": self._last_result})

    async def _gun_preconnect(self, _request: web.Request) -> web.Response:
        try:
            result = await self._gun.preconnect()
        except Exception as exc:  # noqa: BLE001
            log.exception("gun preconnect failed")
            return web.json_response({"ok": False, "result": f"gun preconnect failed: {exc}"}, status=400)
        self._last_result = result
        return web.json_response({"ok": True, "result": result})

    async def _gun_test(self, _request: web.Request) -> web.Response:
        try:
            result = await self._gun.test()
        except Exception as exc:  # noqa: BLE001
            log.exception("gun relay test failed")
            return web.json_response({"ok": False, "result": f"gun relay test failed: {exc}"}, status=400)
        self._last_result = f"gun relay test: {result}"
        return web.json_response({"ok": True, "result": self._last_result})

    async def _gun_status(self, _request: web.Request) -> web.Response:
        try:
            result = await self._gun.status()
        except Exception as exc:  # noqa: BLE001
            log.exception("gun status failed")
            return web.json_response({"ok": False, "result": f"gun status failed: {exc}"}, status=400)
        self._last_result = result
        return web.json_response({"ok": True, "result": result, "gun": self._gun.snapshot()})

    async def _gun_fire(self, _request: web.Request) -> web.Response:
        try:
            result = await self._gun.fire()
        except Exception as exc:  # noqa: BLE001
            log.exception("gun fire failed")
            return web.json_response({"ok": False, "result": f"gun fire failed: {exc}"}, status=400)
        self._last_result = result
        return web.json_response({"ok": True, "result": result})

    async def _gun_stop(self, _request: web.Request) -> web.Response:
        try:
            result = await self._gun.stop()
        except Exception as exc:  # noqa: BLE001
            log.exception("gun stop failed")
            return web.json_response({"ok": False, "result": f"gun stop failed: {exc}"}, status=400)
        self._last_result = result
        return web.json_response({"ok": True, "result": result})

    async def _safe_stop(self) -> None:
        if self._client is not None:
            await self._client.stop()

    async def _perception_loop(self) -> None:
        assert self._perception is not None
        while True:
            try:
                self._perception_health = await self._perception.health()
                self._latest_observation = await self._perception.observe()
            except Exception as exc:  # noqa: BLE001
                log.exception("person perception failed")
                self._perception_health = PerceptionHealth(False, "yolo", str(exc))
                self._latest_observation = Observation(
                    timestamp=time.time(),
                    frame_available=self._latest_jpeg is not None,
                    note=str(exc),
                )
            await asyncio.sleep(_FOLLOW_INTERVAL_S)

    async def _follow_action(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        if action == "start":
            if self._follow is None or self._perception is None:
                return web.json_response({"ok": False, "result": "person follow is disabled"}, status=503)
            if not self._perception_health.ready:
                return web.json_response(
                    {"ok": False, "result": f"person detector not ready: {self._perception_health.detail}"},
                    status=503,
                )
            if self._follow_task is None or self._follow_task.done():
                self._follow_task = asyncio.create_task(self._follow_loop(), name="go2-local-human-follow")
            self._follow_last_action = "started"
            return web.json_response({"ok": True, "result": "person follow started"})
        if action == "stop":
            await self._stop_follow()
            return web.json_response({"ok": True, "result": "person follow stopped"})
        if action == "step":
            await self._follow_step()
            return web.json_response({"ok": True, "result": self._follow_last_action})
        return web.json_response({"ok": False, "result": f"unknown follow action {action!r}"}, status=400)

    async def _follow_loop(self) -> None:
        while True:
            await self._follow_step()
            await asyncio.sleep(_FOLLOW_INTERVAL_S)

    async def _follow_step(self) -> None:
        if self._follow is None:
            self._follow_last_action = "not ready"
            return
        command = await self._follow.step(self._latest_observation)
        self._follow_last_action = command.reason

    async def _stop_follow(self, *, stop_robot: bool = True) -> None:
        task = self._follow_task
        self._follow_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if stop_robot and task is not None:
            await self._safe_stop()
        if task is not None:
            self._follow_last_action = "stopped"

    def _status_payload(self) -> dict[str, Any]:
        sport_state = getattr(self._client, "_sport_state", {}) if self._client is not None else {}
        self._battery_percent = _extract_battery_percent(sport_state)
        people = []
        for detection in self._latest_observation.detections:
            if not detection.is_human():
                continue
            payload = detection_to_dict(
                detection,
                self._latest_observation.frame_width,
                self._latest_observation.frame_height,
            )
            box = payload.get("box")
            if isinstance(box, dict):
                people.append(
                    {
                        "x": box["left"], "y": box["top"], "w": box["width"], "h": box["height"],
                        "label": "person", "score": detection.confidence,
                    }
                )
        return {
            "status": self._status,
            "video_frames": self._video_frames,
            "last_result": self._last_result,
            "faces": self._faces,
            "video_size": self._video_size,
            "face_backend": self._face_backend,
            "face_detector": self._face_detector_name,
            "face_error": self._face_error,
            "face_db": str(self._face_db_path),
            "people": people,
            "person_detector": self._perception_health.__dict__,
            "follow": {
                "enabled": _FOLLOW_ENABLED,
                "active": self._follow_task is not None and not self._follow_task.done(),
                "last_action": self._follow_last_action,
                "last_target": self._follow.last_target if self._follow is not None else "none",
                "last_command": self._follow.last_command.__dict__ if self._follow is not None else None,
            },
            "sport_commands": self._available_sport_commands,
            "gun_active": self._gun.active,
            "gun": self._gun.snapshot(),
            "battery_percent": self._battery_percent,
        }


def _jpeg_from_image(image: Any) -> bytes:
    with io.BytesIO() as out:
        image.save(out, format="JPEG", quality=_JPEG_QUALITY)
        return out.getvalue()


def _face_boxes(image: Any) -> list[tuple[int, int, int, int]]:
    global _FACE_CASCADE
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for live FaceID boxes") from exc

    source = image.convert("RGB")
    width = max(1, int(getattr(source, "width", 1)))
    height = max(1, int(getattr(source, "height", 1)))
    scale = 1.0
    if width > _FACE_DETECT_MAX_WIDTH:
        scale = _FACE_DETECT_MAX_WIDTH / width
        source = source.resize((int(width * scale), int(height * scale)))
    arr = np.asarray(source)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    if _FACE_CASCADE is None:
        _FACE_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if _FACE_CASCADE.empty():
            raise RuntimeError("OpenCV face cascade is empty")
    faces = _FACE_CASCADE.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48))
    return [
        (int(x / scale), int(y / scale), int((x + w) / scale), int((y + h) / scale))
        for x, y, w, h in faces
    ]


def _extract_battery_percent(sport_state: Any) -> float | None:
    if not isinstance(sport_state, dict):
        return None
    candidates: list[Any] = [
        sport_state.get("battery_percent"),
        sport_state.get("battery"),
        sport_state.get("soc"),
        sport_state.get("power_percent"),
    ]
    for key in ("bms_state", "battery_state", "power_state"):
        nested = sport_state.get(key)
        if isinstance(nested, dict):
            candidates.extend([
                nested.get("soc"),
                nested.get("percentage"),
                nested.get("percent"),
                nested.get("battery_percent"),
            ])
    for value in candidates:
        try:
            pct = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 <= pct <= 1.0:
            return round(pct * 100.0, 1)
        if 0.0 <= pct <= 100.0:
            return round(pct, 1)
    return None


async def _json_or_empty(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Go2 Cockpit</title>
  <style>
    :root { color-scheme: dark; --bg:#0b0d0f; --rail:#15191d; --panel:#1d2329; --panel2:#12161a; --line:#343d46; --text:#f3f5f7; --muted:#98a4ad; --red:#d84444; --red2:#7b2424; --blue:#2f7fc3; --green:#41a36c; --yellow:#d9b84f; }
    * { box-sizing:border-box; }
    html, body { margin:0; height:100%; overflow:hidden; background:var(--bg); color:var(--text); font:14px/1.35 Inter, ui-sans-serif, system-ui, Segoe UI, sans-serif; }
    main { height:100vh; display:grid; grid-template-columns:360px minmax(0, 1fr); }
    aside { border-right:1px solid var(--line); background:var(--rail); padding:14px; overflow:auto; }
    header { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px; }
    h1 { margin:0; font-size:19px; line-height:1.1; font-weight:750; }
    h2 { margin:18px 0 8px; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:0; }
    button { min-height:38px; border:1px solid var(--line); border-radius:6px; background:#252c34; color:var(--text); font:inherit; cursor:pointer; }
    button:hover:not(:disabled) { background:#303944; }
    button:active:not(:disabled) { transform:translateY(1px); }
    button:disabled { cursor:not-allowed; opacity:.48; }
    input { min-width:0; min-height:38px; border:1px solid var(--line); border-radius:6px; background:#0f1317; color:var(--text); padding:0 10px; font:inherit; }
    .grid3 { display:grid; grid-template-columns:repeat(3, 1fr); gap:7px; }
    .grid2 { display:grid; grid-template-columns:repeat(2, 1fr); gap:7px; }
    .grid4 { display:grid; grid-template-columns:repeat(4, 1fr); gap:7px; }
    .wide { width:100%; }
    .stop { background:var(--red); border-color:#ff8787; font-weight:700; }
    .fire { background:var(--red2); border-color:#df6969; font-weight:800; }
    .pre { background:#243d55; border-color:#4b89bc; }
    .ok { background:#244733; border-color:#4c996c; }
    label { color:var(--muted); display:grid; gap:5px; margin:9px 0; }
    input[type=range] { width:100%; }
    .pill { display:inline-flex; align-items:center; gap:7px; min-height:26px; padding:3px 9px; border:1px solid var(--line); border-radius:999px; background:#0f1317; color:var(--muted); font-size:12px; white-space:nowrap; }
    .dot { width:8px; height:8px; border-radius:50%; background:var(--muted); }
    .dot.ready { background:var(--green); }
    .dot.warn { background:var(--yellow); }
    .dot.hot { background:var(--red); }
    .status { min-height:66px; padding:10px; border:1px solid var(--line); border-radius:6px; background:var(--panel2); color:var(--muted); white-space:pre-line; overflow:hidden; }
    .metrics { display:grid; grid-template-columns:repeat(3, 1fr); gap:7px; margin:10px 0 12px; }
    .metric { border:1px solid var(--line); border-radius:6px; background:var(--panel2); padding:8px; min-width:0; }
    .metric b { display:block; font-size:16px; line-height:1.1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .metric span { display:block; color:var(--muted); font-size:11px; margin-top:3px; }
    .section { padding:10px; border:1px solid var(--line); border-radius:6px; background:var(--panel); margin-top:10px; }
    .video { position:relative; min-width:0; min-height:0; background:#050607; display:flex; align-items:center; justify-content:center; overflow:hidden; }
    #video { width:100%; height:100%; object-fit:contain; display:block; }
    #overlay { position:absolute; inset:0; pointer-events:none; }
    .crosshair { position:absolute; left:50%; top:50%; width:74px; height:74px; transform:translate(-50%, -50%); pointer-events:none; opacity:.82; filter:drop-shadow(0 1px 2px rgba(0,0,0,.9)); }
    .crosshair::before, .crosshair::after { content:""; position:absolute; background:rgba(255,255,255,.88); }
    .crosshair::before { left:50%; top:0; bottom:0; width:2px; transform:translateX(-50%); }
    .crosshair::after { top:50%; left:0; right:0; height:2px; transform:translateY(-50%); }
    .crosshair-ring { position:absolute; inset:22px; border:2px solid rgba(255,255,255,.88); border-radius:50%; box-shadow:0 0 0 1px rgba(0,0,0,.55) inset; }
    .crosshair-dot { position:absolute; left:50%; top:50%; width:5px; height:5px; border-radius:50%; background:#f1c94a; transform:translate(-50%, -50%); box-shadow:0 0 0 1px rgba(0,0,0,.75); }
    .box { position:absolute; border:2px solid #f1c94a; box-shadow:0 0 0 1px rgba(0,0,0,.8); color:#111; font-size:12px; font-weight:800; transition:left .24s linear, top .24s linear, width .24s linear, height .24s linear, opacity .18s ease; will-change:left,top,width,height; }
    .box.known { border-color:#55d98a; }
    .box.known span { background:#55d98a; }
    .box.person { border-color:#50a7ff; box-shadow:0 0 0 1px rgba(0,0,0,.8), 0 0 14px rgba(80,167,255,.32); }
    .box.person span { background:#50a7ff; }
    .box.face-selectable { pointer-events:auto; cursor:pointer; }
    .box.selected { outline:3px solid #fff; outline-offset:3px; }
    .box span { background:#f1c94a; padding:1px 4px; position:absolute; left:-2px; top:-20px; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .hint { color:var(--muted); font-size:12px; margin-top:8px; }
    .log { height:132px; overflow:auto; margin-top:8px; padding:8px; border:1px solid var(--line); border-radius:6px; background:#090b0d; color:#b8c0c7; font:11px/1.35 ui-monospace, SFMono-Regular, Consolas, monospace; white-space:pre-wrap; }
    .kbd { font-weight:750; }
    @media (max-width: 860px) { html, body { overflow:auto; } main { min-height:100vh; height:auto; grid-template-columns:1fr; grid-template-rows:auto 68vh; } aside { border-right:0; border-bottom:1px solid var(--line); } }
  </style>
</head>
<body>
  <main>
    <aside>
      <header>
        <div>
          <h1>Go2 Cockpit</h1>
          <div class="hint">local video, drive, Face ID, optional trigger</div>
        </div>
        <div class="pill"><span class="dot" id="stateDot"></span><span id="stateText">starting</span></div>
      </header>
      <div class="status" id="status">starting</div>
      <div class="metrics">
        <div class="metric"><b id="batteryMetric">--</b><span>battery</span></div>
        <div class="metric"><b id="framesMetric">0</b><span>frames</span></div>
        <div class="metric"><b id="gunMetric">idle</b><span>trigger</span></div>
      </div>
      <button class="stop wide" onclick="stopNow()">STOP</button>

      <div class="section">
        <h2>Drive</h2>
        <div class="grid3">
          <span></span><button data-move="forward" class="kbd">W</button><span></span>
          <button data-move="left" class="kbd">A</button><button onclick="stopNow()">Stop</button><button data-move="right" class="kbd">D</button>
          <button data-move="turnLeft" class="kbd">Q</button><button data-move="back" class="kbd">S</button><button data-move="turnRight" class="kbd">E</button>
        </div>
        <button class="wide" onclick="jumpNow()">Jump</button>
        <label>Speed <input id="speed" type="range" min="0.10" max="2.00" step="0.05" value="1.00"></label>
        <label>Turn <input id="turn" type="range" min="0.20" max="2.50" step="0.05" value="1.25"></label>
        <div class="hint">Left stick drives. Right stick turns. Space or Xbox A jumps.</div>
      </div>

      <div class="section">
        <h2>Motion</h2>
        <div class="grid2">
          <button onclick="sport('BalanceStand')">Balance</button>
          <button onclick="sport('RecoveryStand')">Recover</button>
          <button onclick="sport('StandUp')">Stand Up</button>
          <button onclick="sport('StandDown')">Stand Down</button>
          <button onclick="sport('Sit')">Sit</button>
          <button onclick="sport('RiseSit')">Rise</button>
          <button onclick="sport('Stretch')">Stretch</button>
          <button onclick="sport('Hello')">Hello</button>
          <button onclick="sport('FrontJump')">Front Jump</button>
          <button onclick="sport('FrontPounce')">Pounce</button>
          <button onclick="sport('FrontFlip', {data:true})">Front Flip</button>
          <button onclick="sport('BackFlip', {data:true})">Back Flip</button>
          <button onclick="sport('LeftFlip', {data:true})">Left Flip</button>
          <button onclick="sport('Dance1')">Dance 1</button>
          <button onclick="sport('Dance2')">Dance 2</button>
          <button onclick="sport('SpeedLevel', {data:2})">Speed 2</button>
        </div>
      </div>

      <div class="section">
        <h2>USB Trigger</h2>
        <div class="grid2">
          <button class="pre" id="gunReadyBtn" onclick="gunPreconnect()">Connect</button>
          <button id="gunStatusBtn" onclick="gunStatus()">Status</button>
          <button id="gunStopBtn" onclick="gunStop()">Stop Fire</button>
          <button class="fire" id="gunFireBtn" onclick="gunFire()">Start Fire</button>
        </div>
        <div class="hint">Optional relay. Hold right trigger to fire. Release right trigger or press Xbox B to stop.</div>
        <div class="log" id="gunLog">waiting for relay log</div>
      </div>

      <div class="section">
        <h2>Face ID</h2>
        <div class="grid2">
          <button onclick="enrollFace()">Save Face</button>
          <input id="faceName" placeholder="name" autocomplete="off">
        </div>
        <div class="hint" id="faceStatus">waiting</div>
      </div>

      <div class="section">
        <h2>Person Follow</h2>
        <div class="grid2">
          <button class="ok" onclick="followPerson('start')">Start Follow</button>
          <button class="stop" onclick="followPerson('stop')">Stop Follow</button>
        </div>
        <button class="wide" onclick="followPerson('step')">Follow One Step</button>
        <div class="hint" id="followStatus">person detector starting</div>
      </div>
    </aside>
    <section class="video" id="videoPanel">
      <img id="video" src="/video.mjpg" alt="Live robot video">
      <div id="overlay"></div>
      <div class="crosshair" aria-hidden="true"><span class="crosshair-ring"></span><span class="crosshair-dot"></span></div>
    </section>
  </main>
  <script>
    const statusEl = document.getElementById("status");
    const faceStatus = document.getElementById("faceStatus");
    const followStatus = document.getElementById("followStatus");
    const gunLogEl = document.getElementById("gunLog");
    const stateDot = document.getElementById("stateDot");
    const stateText = document.getElementById("stateText");
    const batteryMetric = document.getElementById("batteryMetric");
    const framesMetric = document.getElementById("framesMetric");
    const gunMetric = document.getElementById("gunMetric");
    const gunFireBtn = document.getElementById("gunFireBtn");
    const gunStopBtn = document.getElementById("gunStopBtn");
    const gunReadyBtn = document.getElementById("gunReadyBtn");
    const gunStatusBtn = document.getElementById("gunStatusBtn");
    const active = new Set();
    let tick = null;
    let current = {vx:0, vy:0, vyaw:0};
    let gamepad = {vx:0, vy:0, vyaw:0, active:false};
    let gamepadFireDown = false;
    let gamepadJumpDown = false;
    let gamepadStopDown = false;
    let latestFaces = [];
    let selectedFaceIndex = 0;
    let lastStatus = null;
    let lastMoveSent = 0;
    let moveInFlight = false;
    let pendingMove = false;
    const moveSendIntervalMs = 140;
    const gunBusyStates = new Set(["checking", "starting", "stopping"]);

    function speed() { return Number(document.getElementById("speed").value); }
    function turn() { return Number(document.getElementById("turn").value); }
    async function api(path, body = {}) {
      const res = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
      const data = await res.json().catch(() => ({result:"bad response"}));
      if (data.result) statusEl.textContent = data.result;
      if (data.gun) applyGunState(data.gun);
      if (!res.ok) throw new Error(data.result || "request failed");
      return data;
    }
    function vectorFromActive() {
      const s = speed(), t = turn();
      let vx = 0, vy = 0, vyaw = 0;
      if (active.has("forward")) vx += s;
      if (active.has("back")) vx -= s * 0.85;
      if (active.has("left")) vy += s * 0.75;
      if (active.has("right")) vy -= s * 0.75;
      if (active.has("turnLeft")) vyaw += t;
      if (active.has("turnRight")) vyaw -= t;
      vx += gamepad.vx;
      vy += gamepad.vy;
      vyaw += gamepad.vyaw;
      return {vx, vy, vyaw, duration_s:0.20};
    }
    function smoothToward(target) {
      const gain = 0.28;
      current.vx += (target.vx - current.vx) * gain;
      current.vy += (target.vy - current.vy) * gain;
      current.vyaw += (target.vyaw - current.vyaw) * gain;
      return {vx:current.vx, vy:current.vy, vyaw:current.vyaw, duration_s:0.20};
    }
    function pulseMove(force = false) {
      const body = smoothToward(vectorFromActive());
      if (Math.abs(body.vx) <= 0.01 && Math.abs(body.vy) <= 0.01 && Math.abs(body.vyaw) <= 0.01) {
        return;
      }
      const now = performance.now();
      if (!force && now - lastMoveSent < moveSendIntervalMs) {
        pendingMove = true;
        return;
      }
      if (moveInFlight) {
        pendingMove = true;
        return;
      }
      moveInFlight = true;
      pendingMove = false;
      lastMoveSent = now;
      api("/api/move", body)
        .catch(() => {})
        .finally(() => {
          moveInFlight = false;
          if (pendingMove && (active.size > 0 || gamepad.active)) {
            pendingMove = false;
            pulseMove(true);
          }
        });
    }
    function hold(name) {
      active.add(name);
      pulseMove(true);
      if (!tick) tick = setInterval(pulseMove, 150);
    }
    function release(name) {
      active.delete(name);
      if (active.size === 0 && !gamepad.active) stopNow();
    }
    function stopNow() {
      active.clear();
      if (tick) clearInterval(tick);
      tick = null;
      pendingMove = false;
      current = {vx:0, vy:0, vyaw:0};
      return api("/api/stop").catch(() => {});
    }
    document.querySelectorAll("[data-move]").forEach((button) => {
      const name = button.dataset.move;
      button.addEventListener("mousedown", () => hold(name));
      button.addEventListener("mouseup", () => release(name));
      button.addEventListener("mouseleave", () => release(name));
      button.addEventListener("touchstart", (e) => { e.preventDefault(); hold(name); });
      button.addEventListener("touchend", (e) => { e.preventDefault(); release(name); });
    });
    function jumpNow() {
      stopNow();
      return api("/api/jump").catch(() => {});
    }
    const keyMap = {w:"forward", s:"back", a:"left", d:"right", q:"turnLeft", e:"turnRight"};
    function isTyping(event) {
      return ["INPUT", "TEXTAREA", "SELECT"].includes(event.target?.tagName);
    }
    document.addEventListener("keydown", (e) => {
      if (isTyping(e)) return;
      if (e.code === "Space" && !e.repeat) {
        e.preventDefault();
        jumpNow();
        return;
      }
      const name = keyMap[e.key.toLowerCase()];
      if (!name || active.has(name)) return;
      e.preventDefault();
      hold(name);
    });
    document.addEventListener("keyup", (e) => {
      if (isTyping(e)) return;
      const name = keyMap[e.key.toLowerCase()];
      if (!name) return;
      e.preventDefault();
      release(name);
    });

    function gunPreconnect() { return api("/api/gun/preconnect").catch(() => {}); }
    function gunTest() { return api("/api/gun/test").catch(() => {}); }
    function gunStatus() { return api("/api/gun/status").catch(() => {}); }
    function gunFire() { return api("/api/gun/fire").catch(() => {}); }
    function gunStop() { return api("/api/gun/stop").catch(() => {}); }
    function sport(name, parameter = null) { return api("/api/sport", {name, parameter}).catch(() => {}); }
    function followPerson(action) { return api(`/api/follow/${action}`).catch(() => {}); }
    function enrollFace() {
      const input = document.getElementById("faceName");
      let label = input.value.trim();
      if (!label) {
        label = prompt("Name this face:")?.trim() || "";
        if (label) input.value = label;
      }
      if (!label) {
        statusEl.textContent = "enter a face name first";
        return;
      }
      return api("/api/face/enroll", {label, index:selectedFaceIndex}).catch(() => {});
    }
    function deadzone(value, zone = 0.18) {
      return Math.abs(value) < zone ? 0 : value;
    }
    function buttonDown(pad, index, threshold = 0.35) {
      const button = pad.buttons[index];
      if (!button) return false;
      return Boolean(button.pressed) || Number(button.value || 0) >= threshold;
    }
    function rightTriggerDown(pad, wasDown = false) {
      const button = pad.buttons[7];
      if (button?.pressed) return true;
      const value = Number(button?.value || 0);
      if (value >= (wasDown ? 0.12 : 0.32)) return true;
      const axis = pad.axes[5];
      if (typeof axis === "number") {
        return axis > (wasDown ? 0.20 : 0.55);
      }
      return false;
    }
    function pollGamepad() {
      const pads = navigator.getGamepads ? navigator.getGamepads() : [];
      const pad = Array.from(pads).find(Boolean);
      if (!pad) {
        if (gamepadFireDown) {
          gamepadFireDown = false;
          gunStop();
        }
        if (gamepad.active) {
          gamepad = {vx:0, vy:0, vyaw:0, active:false};
          stopNow();
        }
        requestAnimationFrame(pollGamepad);
        return;
      }

      const s = speed();
      const t = turn();
      const lx = deadzone(pad.axes[0] || 0);
      const ly = deadzone(pad.axes[1] || 0);
      const rx = deadzone(pad.axes[2] || 0);
      gamepad = {
        vx: -ly * s,
        vy: -lx * s * 0.75,
        vyaw: -rx * t,
        active: Boolean(lx || ly || rx),
      };
      if (gamepad.active) pulseMove();

      const rt = rightTriggerDown(pad, gamepadFireDown);
      if (rt && !gamepadFireDown) gunFire();
      if (!rt && gamepadFireDown) gunStop();
      gamepadFireDown = rt;

      const a = buttonDown(pad, 0);
      if (a && !gamepadJumpDown) jumpNow();
      gamepadJumpDown = a;

      const b = buttonDown(pad, 1);
      if (b && !gamepadStopDown) gunStop();
      gamepadStopDown = b;

      requestAnimationFrame(pollGamepad);
    }
    window.addEventListener("gamepadconnected", (event) => {
      statusEl.textContent = `gamepad connected: ${event.gamepad.id}`;
    });
    pollGamepad();

    function drawFaces(data) {
      const panel = document.getElementById("videoPanel");
      const img = document.getElementById("video");
      const overlay = document.getElementById("overlay");
      const faces = data.faces || [];
      const people = (data.people || []).map((person) => ({...person, person:true}));
      const visionItems = [...faces, ...people];
      latestFaces = faces;
      const panelRect = panel.getBoundingClientRect();
      const imgRatio = (data.video_size?.width || 1) / (data.video_size?.height || 1);
      const panelRatio = panelRect.width / Math.max(1, panelRect.height);
      let drawW = panelRect.width, drawH = panelRect.height, offX = 0, offY = 0;
      if (panelRatio > imgRatio) {
        drawW = panelRect.height * imgRatio;
        offX = (panelRect.width - drawW) / 2;
      } else {
        drawH = panelRect.width / imgRatio;
        offY = (panelRect.height - drawH) / 2;
      }
      visionItems.forEach((face, index) => {
        let box = overlay.children[index];
        if (!box) {
          box = document.createElement("div");
          box.innerHTML = "<span></span>";
          overlay.appendChild(box);
        }
        box.className = "box";
        if (face.known) box.classList.add("known");
        if (face.person) box.classList.add("person");
        if (!face.person) {
          box.classList.add("face-selectable");
          if (index === selectedFaceIndex) box.classList.add("selected");
          box.onclick = () => { selectedFaceIndex = index; drawFaces(lastStatus || data); };
        } else {
          box.onclick = null;
        }
        box.style.left = `${offX + face.x * drawW}px`;
        box.style.top = `${offY + face.y * drawH}px`;
        box.style.width = `${face.w * drawW}px`;
        box.style.height = `${face.h * drawH}px`;
        const score = typeof face.score === "number" ? ` ${face.score.toFixed(2)}` : "";
        box.firstElementChild.textContent = `${face.label || "face"}${score}`;
      });
      while (overlay.children.length > visionItems.length) overlay.lastElementChild.remove();
      if (selectedFaceIndex >= faces.length) selectedFaceIndex = Math.max(0, faces.length - 1);
      const names = faces.map((face) => face.label || "face").join(", ");
      const engine = `${data.face_detector || "face"} + ${data.face_backend || "labels"}`;
      faceStatus.textContent = `${engine} / ${faces.length} visible${names ? ": " + names : ""}${faces.length > 1 ? ` / selected face ${selectedFaceIndex + 1}` : ""}${data.face_error ? " / " + data.face_error : ""}`;
      const follow = data.follow || {};
      const detector = data.person_detector || {};
      const command = follow.last_command || {};
      followStatus.textContent = `${follow.active ? "FOLLOWING" : "idle"} / ${people.length} people / ${detector.ready ? "detector ready" : detector.detail || "detector loading"} / ${follow.last_action || "idle"}${follow.active ? ` / vx ${Number(command.vx || 0).toFixed(2)} yaw ${Number(command.vyaw || 0).toFixed(2)}` : ""}`;
    }
    function applyGunState(gun) {
      const state = gun?.state || (gun?.active ? "firing" : "idle");
      const busy = gunBusyStates.has(state);
      gunMetric.textContent = state;
      gunFireBtn.disabled = busy || state === "firing";
      gunStopBtn.disabled = state === "stopping";
      gunReadyBtn.disabled = busy;
      gunStatusBtn.disabled = busy;
      gunLogEl.textContent = (gun?.log_tail || []).join("\\n") || "no relay log yet";
    }
    async function refreshStatus() {
      const res = await fetch("/status.json");
      const data = await res.json();
      lastStatus = data;
      const battery = data.battery_percent == null ? "unknown" : `${data.battery_percent}%`;
      const gun = data.gun || {};
      const gunState = gun.state || (data.gun_active ? "firing" : "idle");
      stateText.textContent = data.status || "unknown";
      stateDot.className = `dot ${data.status === "connected" ? "ready" : data.status === "starting" || data.status === "connecting" ? "warn" : "hot"}`;
      batteryMetric.textContent = battery;
      framesMetric.textContent = String(data.video_frames || 0);
      statusEl.textContent = `${data.status} / gun ${gunState}\\n${data.last_result || gun.last_result || ""}${gun.last_error ? "\\n" + gun.last_error : ""}`;
      applyGunState(gun);
      drawFaces(data);
    }
    setInterval(refreshStatus, 500);
    refreshStatus();
  </script>
</body>
</html>
"""


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Localhost-only Go2 cockpit with WASD, video, FaceID, and USB trigger")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8775)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await LocalCockpit(args.host, args.port).run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
