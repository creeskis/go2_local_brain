"""Localhost-only WASD/video/face/gun cockpit."""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import time
from typing import Any

from aiohttp import web

from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient
from .gun_relay import GunRelay, gun_relay_config_from_env
from .viewer import _jpeg_from_frame

log = logging.getLogger(__name__)

_MOVE_DURATION_S = 0.28
_FACE_INTERVAL_S = 0.20


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
        self._faces: list[dict[str, float]] = []
        self._video_size = {"width": 0, "height": 0}
        self._face_error = ""
        self._last_face_scan = 0.0
        self._face_cascade: Any = None
        self._face_backend = "not-loaded"

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
        app.router.add_post("/api/gun/fire", self._gun_fire)
        app.router.add_post("/api/gun/stop", self._gun_stop)

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
            self._video_size = {"width": int(getattr(image, "width", 0)), "height": int(getattr(image, "height", 0))}
            now = time.monotonic()
            if now - self._last_face_scan >= _FACE_INTERVAL_S:
                self._last_face_scan = now
                self._faces = self._detect_faces(image)
            jpeg = _jpeg_from_image(image)
            async with self._state_changed:
                self._latest_jpeg = jpeg
                self._latest_video_ts = time.time()
                self._video_frames += 1
                self._state_changed.notify_all()

    def _load_face_detector(self) -> None:
        try:
            import cv2  # type: ignore

            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            cascade = cv2.CascadeClassifier(cascade_path)
            if cascade.empty():
                self._face_backend = "opencv-unavailable"
                self._face_error = "empty cascade"
                return
            self._face_cascade = cascade
            self._face_backend = "opencv-haar"
        except Exception as exc:  # noqa: BLE001
            self._face_backend = "opencv-unavailable"
            self._face_error = str(exc)

    def _detect_faces(self, image: Any) -> list[dict[str, float]]:
        if self._face_cascade is None:
            return []
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore

            rgb = np.array(image.convert("RGB"))
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            faces = self._face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(32, 32))
            width = max(1, int(getattr(image, "width", 1)))
            height = max(1, int(getattr(image, "height", 1)))
            return [
                {
                    "x": float(x) / width,
                    "y": float(y) / height,
                    "w": float(w) / width,
                    "h": float(h) / height,
                    "label": "face",
                }
                for x, y, w, h in faces[:8]
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
        return {
            "status": self._status,
            "video_frames": self._video_frames,
            "last_result": self._last_result,
            "faces": self._faces,
            "video_size": self._video_size,
            "face_backend": self._face_backend,
            "face_error": self._face_error,
            "gun_active": self._gun.active,
        }


def _jpeg_from_image(image: Any) -> bytes:
    with io.BytesIO() as out:
        image.save(out, format="JPEG", quality=80)
        return out.getvalue()


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
      <label>Speed <input id="speed" type="range" min="0.10" max="0.75" step="0.05" value="0.40"></label>
      <label>Turn <input id="turn" type="range" min="0.20" max="1.10" step="0.05" value="0.75"></label>

      <h2>USB Trigger</h2>
      <div class="grid2">
        <button class="pre" onclick="gunPreconnect()">SSH Warm</button>
        <button onclick="gunStop()">Stop Fire</button>
      </div>
      <button class="fire wide" id="fireBtn">Hold Fire</button>
      <div class="hint">Fire starts the remote USB command through the dog SSH jump. Release or Stop Fire sends Ctrl+C/terminates it.</div>

      <h2>FaceID</h2>
      <div class="hint" id="faceStatus">waiting</div>
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
      if (active.has("back")) vx -= s * 0.75;
      if (active.has("left")) vy += s * 0.6;
      if (active.has("right")) vy -= s * 0.6;
      if (active.has("turnLeft")) vyaw += t;
      if (active.has("turnRight")) vyaw -= t;
      return {vx, vy, vyaw, duration_s:0.28};
    }
    function pulseMove() {
      const body = vectorFromActive();
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
    function gunFire() { return api("/api/gun/fire").catch(() => {}); }
    function gunStop() { return api("/api/gun/stop").catch(() => {}); }
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
        box.innerHTML = "<span>FaceID</span>";
        overlay.appendChild(box);
      });
      faceStatus.textContent = `${data.face_backend || "none"} faces=${faces.length}${data.face_error ? " error=" + data.face_error : ""}`;
    }
    async function refreshStatus() {
      const res = await fetch("/status.json");
      const data = await res.json();
      statusEl.textContent = `${data.status} video=${data.video_frames} gun=${data.gun_active ? "active" : "idle"}\\n${data.last_result || ""}`;
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
