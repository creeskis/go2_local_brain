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
from .autonomy.face_id import FaceDatabase, FaceIdentifier, UNKNOWN_LABEL, build_face_embedder
from .driver.webrtc_client import Go2Config, Go2WebRTCClient
from .gun_relay import GunRelay, gun_relay_config_from_env

log = logging.getLogger(__name__)

_MOVE_DURATION_S = 0.20
_FACE_INTERVAL_S = 0.20
_DEFAULT_FACE_BACKEND = "face_recognition"


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
        self._face_db_path = Path(os.getenv("GO2_FACE_DB", str(FaceDatabase.default_path()))).expanduser()
        self._latest_image: Any = None
        self._battery_percent: float | None = None

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
        app.router.add_post("/api/gun/preconnect", self._gun_preconnect)
        app.router.add_post("/api/gun/test", self._gun_test)
        app.router.add_post("/api/gun/fire", self._gun_fire)
        app.router.add_post("/api/gun/stop", self._gun_stop)
        app.router.add_post("/api/face/enroll", self._face_enroll)

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
        self._attach_video()
        self._status = "connected"

    async def _shutdown(self) -> None:
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
            if now - self._last_face_scan >= _FACE_INTERVAL_S:
                self._last_face_scan = now
                self._faces = self._identify_faces(self._latest_image)
            jpeg = _jpeg_from_image(image)
            async with self._state_changed:
                self._latest_jpeg = jpeg
                self._latest_video_ts = time.time()
                self._video_frames += 1
                self._state_changed.notify_all()

    def _load_face_detector(self) -> None:
        try:
            database = FaceDatabase.load_or_empty(self._face_db_path)
            embedder = build_face_embedder(self._face_backend)
            self._face_identifier = FaceIdentifier(embedder, database)
        except Exception as exc:  # noqa: BLE001
            self._face_identifier = None
            self._face_error = str(exc)

    def _identify_faces(self, image: Any) -> list[dict[str, Any]]:
        if self._face_identifier is None:
            return []
        try:
            boxes = _face_boxes(image)
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
                for face in identified[:8]
            ]
        except Exception as exc:  # noqa: BLE001
            self._face_error = str(exc)
            return []

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
        if self._latest_image is None or not self._faces:
            return web.json_response({"ok": False, "result": "no face is visible yet"}, status=400)
        face = self._faces[min(max(index, 0), len(self._faces) - 1)]
        width = max(1, int(getattr(self._latest_image, "width", 1)))
        height = max(1, int(getattr(self._latest_image, "height", 1)))
        box = (
            int(face["x"] * width),
            int(face["y"] * height),
            int((face["x"] + face["w"]) * width),
            int((face["y"] + face["h"]) * height),
        )
        try:
            ok = self._face_identifier.enroll_from_image(label, self._latest_image, box)
            if not ok:
                return web.json_response({"ok": False, "result": "face embedding failed"}, status=400)
            path = self._face_identifier.database.save(self._face_db_path)
        except Exception as exc:  # noqa: BLE001
            log.exception("face enroll failed")
            return web.json_response({"ok": False, "result": f"face enroll failed: {exc}"}, status=400)
        self._last_result = f"enrolled face {label}"
        return web.json_response({"ok": True, "result": f"enrolled {label} -> {path}"})

    async def _stop_action(self, _request: web.Request) -> web.Response:
        await self._safe_stop()
        self._last_result = "stop"
        return web.json_response({"ok": True, "result": "stop"})

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

    async def _gun_fire(self, _request: web.Request) -> web.Response:
        try:
            result = await self._gun.fire()
        except Exception as exc:  # noqa: BLE001
            log.exception("gun fire failed")
            return web.json_response({"ok": False, "result": f"gun fire failed: {exc}"}, status=400)
        self._last_result = result
        return web.json_response({"ok": True, "result": result})

    async def _gun_stop(self, _request: web.Request) -> web.Response:
        result = await self._gun.stop()
        self._last_result = result
        return web.json_response({"ok": True, "result": result})

    async def _safe_stop(self) -> None:
        if self._client is not None:
            await self._client.stop()

    def _status_payload(self) -> dict[str, Any]:
        sport_state = getattr(self._client, "_sport_state", {}) if self._client is not None else {}
        self._battery_percent = _extract_battery_percent(sport_state)
        return {
            "status": self._status,
            "video_frames": self._video_frames,
            "last_result": self._last_result,
            "faces": self._faces,
            "video_size": self._video_size,
            "face_backend": self._face_backend,
            "face_error": self._face_error,
            "face_db": str(self._face_db_path),
            "gun_active": self._gun.active,
            "battery_percent": self._battery_percent,
        }


def _jpeg_from_image(image: Any) -> bytes:
    with io.BytesIO() as out:
        image.save(out, format="JPEG", quality=80)
        return out.getvalue()


def _face_boxes(image: Any) -> list[tuple[int, int, int, int]]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for live FaceID boxes") from exc

    arr = np.asarray(image.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48))
    return [(int(x), int(y), int(x + w), int(y + h)) for x, y, w, h in faces]


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
  <title>Go2 Local Cockpit</title>
  <style>
    :root { color-scheme: dark; --bg:#0f1114; --panel:#171b20; --line:#303841; --text:#f4f7fa; --muted:#9aa6b2; --red:#e64a4a; --blue:#3c91e6; --green:#46b06b; }
    * { box-sizing:border-box; }
    body { margin:0; height:100vh; overflow:hidden; background:var(--bg); color:var(--text); font:14px/1.35 system-ui, Segoe UI, sans-serif; }
    main { height:100vh; display:grid; grid-template-columns:330px 1fr; }
    aside { border-right:1px solid var(--line); background:var(--panel); padding:12px; overflow:auto; }
    h1 { margin:0 0 8px; font-size:18px; }
    h2 { margin:18px 0 8px; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:0; }
    button { min-height:38px; border:1px solid var(--line); border-radius:6px; background:#252c34; color:var(--text); font:inherit; cursor:pointer; }
    button:hover { background:#303944; }
    button:active { transform:translateY(1px); }
    .grid3 { display:grid; grid-template-columns:repeat(3, 1fr); gap:7px; }
    .grid2 { display:grid; grid-template-columns:repeat(2, 1fr); gap:7px; }
    .wide { width:100%; }
    .stop { background:var(--red); border-color:#ff8787; font-weight:700; }
    .fire { background:#842727; border-color:#e36b6b; font-weight:800; }
    .pre { background:#284769; border-color:#4d8dcc; }
    label { color:var(--muted); display:grid; gap:5px; margin:8px 0; }
    input[type=range] { width:100%; }
    .status { color:var(--muted); min-height:48px; white-space:pre-line; }
    .video { position:relative; min-width:0; min-height:0; background:#050607; display:flex; align-items:center; justify-content:center; overflow:hidden; }
    #video { width:100%; height:100%; object-fit:contain; display:block; }
    #overlay { position:absolute; inset:0; pointer-events:none; }
    .box { position:absolute; border:2px solid #ffd447; box-shadow:0 0 0 1px rgba(0,0,0,.8); color:#111; font-size:12px; font-weight:800; }
    .box span { background:#ffd447; padding:1px 4px; position:absolute; left:-2px; top:-20px; }
    .hint { color:var(--muted); font-size:12px; margin-top:8px; }
    @media (max-width: 860px) { main { grid-template-columns:1fr; grid-template-rows:auto 1fr; } aside { border-right:0; border-bottom:1px solid var(--line); } }
  </style>
</head>
<body>
  <main>
    <aside>
      <h1>Go2 Local Cockpit</h1>
      <div class="status" id="status">starting</div>
      <button class="stop wide" onclick="stopNow()">STOP</button>

      <h2>WASD</h2>
      <div class="grid3">
        <span></span><button data-move="forward">W</button><span></span>
        <button data-move="left">A</button><button onclick="stopNow()">Stop</button><button data-move="right">D</button>
        <button data-move="turnLeft">Q</button><button data-move="back">S</button><button data-move="turnRight">E</button>
      </div>
      <label>Speed <input id="speed" type="range" min="0.10" max="2.00" step="0.05" value="0.65"></label>
      <label>Turn <input id="turn" type="range" min="0.20" max="2.00" step="0.05" value="0.95"></label>

      <h2>USB Trigger</h2>
      <div class="grid2">
        <button class="pre" onclick="gunPreconnect()">SSH Warm</button>
        <button onclick="gunTest()">Test SSH</button>
        <button onclick="gunStop()">Stop Fire</button>
      </div>
      <button class="fire wide" id="fireBtn">Hold Fire</button>
      <div class="hint">Fire starts the remote USB command through the dog SSH jump. Release or Stop Fire sends Ctrl+C/terminates it.</div>

      <h2>FaceID</h2>
      <div class="hint" id="faceStatus">waiting</div>
      <div class="grid2">
        <button onclick="enrollFace()">Add Face To DB</button>
        <input id="faceName" placeholder="face name" autocomplete="off">
      </div>
      <div class="hint">Type a name, keep one face visible, then click Add Face To DB.</div>
    </aside>
    <section class="video" id="videoPanel">
      <img id="video" src="/video.mjpg" alt="Live robot video">
      <div id="overlay"></div>
    </section>
  </main>
  <script>
    const statusEl = document.getElementById("status");
    const faceStatus = document.getElementById("faceStatus");
    const active = new Set();
    let tick = null;
    let current = {vx:0, vy:0, vyaw:0};
    let latestFaces = [];

    function speed() { return Number(document.getElementById("speed").value); }
    function turn() { return Number(document.getElementById("turn").value); }
    async function api(path, body = {}) {
      const res = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
      const data = await res.json().catch(() => ({result:"bad response"}));
      if (data.result) statusEl.textContent = data.result;
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
      return {vx, vy, vyaw, duration_s:0.28};
    }
    function smoothToward(target) {
      const gain = 0.42;
      current.vx += (target.vx - current.vx) * gain;
      current.vy += (target.vy - current.vy) * gain;
      current.vyaw += (target.vyaw - current.vyaw) * gain;
      return {vx:current.vx, vy:current.vy, vyaw:current.vyaw, duration_s:0.20};
    }
    function pulseMove() {
      const body = smoothToward(vectorFromActive());
      if (body.vx || body.vy || body.vyaw) api("/api/move", body).catch(() => {});
    }
    function hold(name) {
      active.add(name);
      pulseMove();
      if (!tick) tick = setInterval(pulseMove, 210);
    }
    function release(name) {
      active.delete(name);
      if (active.size === 0) stopNow();
    }
    function stopNow() {
      active.clear();
      if (tick) clearInterval(tick);
      tick = null;
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
    const keyMap = {w:"forward", s:"back", a:"left", d:"right", q:"turnLeft", e:"turnRight"};
    document.addEventListener("keydown", (e) => {
      const name = keyMap[e.key.toLowerCase()];
      if (!name || active.has(name)) return;
      e.preventDefault();
      hold(name);
    });
    document.addEventListener("keyup", (e) => {
      const name = keyMap[e.key.toLowerCase()];
      if (!name) return;
      e.preventDefault();
      release(name);
    });

    function gunPreconnect() { return api("/api/gun/preconnect").catch(() => {}); }
    function gunTest() { return api("/api/gun/test").catch(() => {}); }
    function gunFire() { return api("/api/gun/fire").catch(() => {}); }
    function gunStop() { return api("/api/gun/stop").catch(() => {}); }
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
      return api("/api/face/enroll", {label, index:0}).catch(() => {});
    }
    const fireBtn = document.getElementById("fireBtn");
    fireBtn.addEventListener("mousedown", gunFire);
    fireBtn.addEventListener("mouseup", gunStop);
    fireBtn.addEventListener("mouseleave", gunStop);
    fireBtn.addEventListener("touchstart", (e) => { e.preventDefault(); gunFire(); });
    fireBtn.addEventListener("touchend", (e) => { e.preventDefault(); gunStop(); });

    function drawFaces(data) {
      const panel = document.getElementById("videoPanel");
      const img = document.getElementById("video");
      const overlay = document.getElementById("overlay");
      overlay.innerHTML = "";
      const faces = data.faces || [];
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
      faces.forEach((face) => {
        const box = document.createElement("div");
        box.className = "box";
        box.style.left = `${offX + face.x * drawW}px`;
        box.style.top = `${offY + face.y * drawH}px`;
        box.style.width = `${face.w * drawW}px`;
        box.style.height = `${face.h * drawH}px`;
        const score = typeof face.score === "number" ? ` ${face.score.toFixed(2)}` : "";
        box.innerHTML = `<span>${face.label || "face"}${score}</span>`;
        overlay.appendChild(box);
      });
      faceStatus.textContent = `${data.face_backend || "none"} faces=${faces.length} db=${data.face_db || ""}${data.face_error ? " error=" + data.face_error : ""}`;
    }
    async function refreshStatus() {
      const res = await fetch("/status.json");
      const data = await res.json();
      const battery = data.battery_percent == null ? "unknown" : `${data.battery_percent}%`;
      statusEl.textContent = `${data.status} video=${data.video_frames} battery=${battery} gun=${data.gun_active ? "active" : "idle"}\\n${data.last_result || ""}`;
      drawFaces(data);
    }
    setInterval(refreshStatus, 400);
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
