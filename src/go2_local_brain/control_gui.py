"""Simple browser cockpit: live video, keyboard driving, and sport-command buttons."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from typing import Any

from aiohttp import web

from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient
from .viewer import _jpeg_from_frame

log = logging.getLogger(__name__)

_MOVE_DURATION_S = 0.32


class ControlGui:
    """A deliberately small GUI that leaves AI and LiDAR out of the loop."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._client: Go2WebRTCClient | None = None
        self._state_changed = asyncio.Condition()
        self._latest_jpeg: bytes | None = None
        self._latest_video_ts = 0.0
        self._video_frames = 0
        self._status = "starting"
        self._last_result = ""
        self._available_commands: list[str] = []

    async def run(self) -> None:
        cfg = load_config()
        self._client = Go2WebRTCClient(
            Go2Config(
                ip=cfg.go2_ip,
                aes_128_key=cfg.go2_aes_128_key,
                force_motion_mode=cfg.force_motion_mode,
            )
        )

        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get("/", self._index)
        app.router.add_get("/video.mjpg", self._video_stream)
        app.router.add_get("/status.json", self._status_json)
        app.router.add_post("/api/move", self._move_action)
        app.router.add_post("/api/stop", self._stop_action)
        app.router.add_post("/api/sport", self._sport_action)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("control GUI listening on http://%s:%s", self._host, self._port)

        try:
            await self._connect()
            while True:
                await asyncio.sleep(3600)
        finally:
            await self._shutdown()
            await runner.cleanup()

    async def _connect(self) -> None:
        assert self._client is not None
        self._status = "connecting"
        await self._client.connect()
        self._available_commands = self._client.available_sport_commands()
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
            jpeg = _jpeg_from_frame(frame)
            async with self._state_changed:
                self._latest_jpeg = jpeg
                self._latest_video_ts = time.time()
                self._video_frames += 1
                self._state_changed.notify_all()

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _status_json(self, _request: web.Request) -> web.Response:
        return web.json_response(self._status_payload())

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
            return web.json_response({"ok": False, "result": "client is not connected"}, status=503)
        payload = await _json_or_empty(request)
        try:
            vx = float(payload.get("vx", 0.0))
            vy = float(payload.get("vy", 0.0))
            vyaw = float(payload.get("vyaw", 0.0))
            duration = float(payload.get("duration_s", _MOVE_DURATION_S))
            await self._client.move(vx, vy, vyaw, duration)
            result = f"move vx={vx:.2f} vy={vy:.2f} vyaw={vyaw:.2f}"
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

    async def _sport_action(self, request: web.Request) -> web.Response:
        if self._client is None:
            return web.json_response({"ok": False, "result": "client is not connected"}, status=503)
        payload = await _json_or_empty(request)
        name = str(payload.get("name", "")).strip()
        if not name:
            return web.json_response({"ok": False, "result": "name is required"}, status=400)
        parameter = payload.get("parameter")
        if parameter is not None and not isinstance(parameter, dict):
            return web.json_response({"ok": False, "result": "parameter must be an object"}, status=400)
        try:
            if name == "StopMove":
                await self._client.stop()
            else:
                await self._client.sport_command(name, parameter)
            result = name
        except Exception as exc:  # noqa: BLE001
            log.exception("sport command failed: %s", name)
            await self._safe_stop()
            return web.json_response({"ok": False, "result": f"{name} failed: {exc}"}, status=400)
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
            "available_commands": self._available_commands,
        }


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
  <title>Go2 Control GUI</title>
  <style>
    :root { color-scheme: dark; --bg:#111315; --panel:#191d21; --line:#303740; --text:#f2f5f7; --muted:#9ba7b4; --accent:#4ba3ff; --danger:#e64f4f; }
    * { box-sizing: border-box; }
    body { margin:0; min-height:100vh; background:var(--bg); color:var(--text); font:14px/1.35 system-ui, Segoe UI, sans-serif; }
    main { display:grid; grid-template-columns:minmax(330px, 430px) 1fr; gap:14px; height:100vh; padding:14px; }
    aside { overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
    .video { min-height:0; background:#050607; border:1px solid var(--line); border-radius:8px; overflow:hidden; display:flex; align-items:center; justify-content:center; }
    .video img { width:100%; height:100%; object-fit:contain; display:block; }
    h1 { font-size:18px; margin:0 0 8px; }
    h2 { font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:0; margin:18px 0 8px; }
    .status { color:var(--muted); min-height:40px; }
    button, select, input { font:inherit; }
    button { border:1px solid var(--line); background:#242b32; color:var(--text); border-radius:6px; padding:8px 9px; cursor:pointer; min-height:36px; }
    button:hover { background:#2d3540; }
    button:active { transform:translateY(1px); }
    .stop { background:var(--danger); border-color:#ff8585; color:white; font-weight:700; }
    .grid3 { display:grid; grid-template-columns:repeat(3, 1fr); gap:7px; }
    .grid2 { display:grid; grid-template-columns:repeat(2, 1fr); gap:7px; }
    .grid4 { display:grid; grid-template-columns:repeat(4, 1fr); gap:7px; }
    .wide { grid-column:1 / -1; }
    label { display:grid; gap:5px; color:var(--muted); margin:8px 0; }
    input[type=range] { width:100%; }
    .hint { color:var(--muted); font-size:12px; margin-top:8px; }
    .available { max-height:120px; overflow:auto; border:1px solid var(--line); padding:8px; color:var(--muted); border-radius:6px; }
    @media (max-width: 900px) { main { grid-template-columns:1fr; height:auto; } .video { height:55vh; } }
  </style>
</head>
<body>
  <main>
    <aside>
      <h1>Go2 Control GUI</h1>
      <div class="status" id="status">starting</div>

      <button class="stop wide" onclick="stopNow()">STOP</button>

      <h2>Drive</h2>
      <div class="grid3">
        <span></span><button data-move="forward">Forward</button><span></span>
        <button data-move="left">Left</button><button onclick="sport('BalanceStand')">Balance</button><button data-move="right">Right</button>
        <button data-move="turnLeft">Turn L</button><button data-move="back">Back</button><button data-move="turnRight">Turn R</button>
        <button data-move="walkTurnLeft">Walk+L</button><button onclick="stopNow()">Stop</button><button data-move="walkTurnRight">Walk+R</button>
      </div>
      <label>Speed <input id="speed" type="range" min="0.10" max="0.75" step="0.05" value="0.45"></label>
      <label>Turn <input id="turn" type="range" min="0.20" max="1.10" step="0.05" value="0.85"></label>
      <div class="hint">Keyboard: W/A/S/D move, Q/E turn, combine W+Q or W+E to walk and turn.</div>

      <h2>Core</h2>
      <div class="grid3">
        <button onclick="sport('Damp')">Damp</button>
        <button onclick="sport('BalanceStand')">Balance</button>
        <button onclick="sport('StopMove')">StopMove</button>
        <button onclick="sport('StandUp')">Stand Up</button>
        <button onclick="sport('StandDown')">Stand Down</button>
        <button onclick="sport('RecoveryStand')">Recovery</button>
        <button onclick="sport('Sit')">Sit</button>
        <button onclick="sport('RiseSit')">Rise Sit</button>
        <button onclick="sport('SwitchAvoidMode')">Avoid Mode</button>
      </div>

      <h2>Gestures And Stunts</h2>
      <div class="grid3">
        <button onclick="sport('Hello')">Hello</button>
        <button onclick="sport('Stretch')">Stretch</button>
        <button onclick="sport('Content')">Content</button>
        <button onclick="sport('Heart')">Heart</button>
        <button onclick="sport('Scrape')">Scrape</button>
        <button onclick="sport('FrontPounce')">Pounce</button>
        <button onclick="sport('FrontJump')">Front Jump</button>
        <button onclick="sport('FrontFlip')">Front Flip</button>
        <button onclick="sport('LeftFlip')">Left Flip</button>
        <button onclick="sport('BackFlip')">Back Flip</button>
        <button onclick="sport('Dance1')">Dance 1</button>
        <button onclick="sport('Dance2')">Dance 2</button>
        <button onclick="sport('HandStand', {data:true})">HandStand On</button>
        <button onclick="sport('HandStand', {data:false})">HandStand Off</button>
        <button onclick="sport('BackStand', {data:true})">BackStand</button>
      </div>

      <h2>Gaits And Modes</h2>
      <div class="grid3">
        <button onclick="sport('FreeWalk')">FreeWalk</button>
        <button onclick="sport('StaticWalk')">StaticWalk</button>
        <button onclick="sport('TrotRun')">TrotRun</button>
        <button onclick="sport('EconomicGait')">Economic</button>
        <button onclick="sport('FreeBound', {data:true})">FreeBound On</button>
        <button onclick="sport('FreeBound', {data:false})">FreeBound Off</button>
        <button onclick="sport('FreeJump', {data:true})">FreeJump On</button>
        <button onclick="sport('FreeJump', {data:false})">FreeJump Off</button>
        <button onclick="sport('FreeAvoid', {data:true})">FreeAvoid On</button>
        <button onclick="sport('FreeAvoid', {data:false})">FreeAvoid Off</button>
        <button onclick="sport('ClassicWalk', {data:true})">Classic On</button>
        <button onclick="sport('ClassicWalk', {data:false})">Classic Off</button>
        <button onclick="sport('WalkUpright', {data:true})">Upright On</button>
        <button onclick="sport('WalkUpright', {data:false})">Upright Off</button>
        <button onclick="sport('CrossStep', {data:true})">CrossStep On</button>
        <button onclick="sport('CrossStep', {data:false})">CrossStep Off</button>
      </div>

      <h2>Toggles</h2>
      <div class="grid2">
        <button onclick="sport('Pose', {data:true})">Pose On</button>
        <button onclick="sport('Pose', {data:false})">Pose Off</button>
        <button onclick="sport('SwitchJoystick', {data:true})">Joystick On</button>
        <button onclick="sport('SwitchJoystick', {data:false})">Joystick Off</button>
        <button onclick="sport('AutoRecoverSet', {data:true})">AutoRecover On</button>
        <button onclick="sport('AutoRecoverSet', {data:false})">AutoRecover Off</button>
      </div>

      <h2>Speed Level</h2>
      <div class="grid4">
        <button onclick="sport('SpeedLevel', {data:0})">0</button>
        <button onclick="sport('SpeedLevel', {data:1})">1</button>
        <button onclick="sport('SpeedLevel', {data:2})">2</button>
        <button onclick="sport('SpeedLevel', {data:3})">3</button>
      </div>

      <h2>Installed Commands</h2>
      <div class="available" id="available">waiting</div>
    </aside>
    <section class="video"><img id="video" src="/video.mjpg" alt="Live robot video"></section>
  </main>
  <script>
    const resultEl = document.getElementById("status");
    const active = new Set();
    let tick = null;

    function speed() { return Number(document.getElementById("speed").value); }
    function turn() { return Number(document.getElementById("turn").value); }

    async function api(path, body = {}) {
      const res = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
      const data = await res.json().catch(() => ({result:"bad response"}));
      resultEl.textContent = data.result || "";
      if (!res.ok) throw new Error(data.result || "request failed");
      return data;
    }
    function sport(name, parameter = null) { return api("/api/sport", {name, parameter}); }
    function stopNow() { active.clear(); clearInterval(tick); tick = null; return api("/api/stop"); }

    function vectorFromActive() {
      const s = speed(), t = turn();
      let vx = 0, vy = 0, vyaw = 0;
      if (active.has("forward")) vx += s;
      if (active.has("back")) vx -= s * 0.75;
      if (active.has("left")) vy += s * 0.6;
      if (active.has("right")) vy -= s * 0.6;
      if (active.has("turnLeft")) vyaw += t;
      if (active.has("turnRight")) vyaw -= t;
      if (active.has("walkTurnLeft")) { vx += s * 0.75; vyaw += t * 0.75; }
      if (active.has("walkTurnRight")) { vx += s * 0.75; vyaw -= t * 0.75; }
      return {vx, vy, vyaw, duration_s:0.32};
    }
    function pulseMove() {
      const body = vectorFromActive();
      if (body.vx || body.vy || body.vyaw) api("/api/move", body).catch(() => {});
    }
    function hold(name) {
      active.add(name);
      pulseMove();
      if (!tick) tick = setInterval(pulseMove, 240);
    }
    function release(name) {
      active.delete(name);
      if (active.size === 0) stopNow();
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

    async function refreshStatus() {
      const res = await fetch("/status.json");
      const data = await res.json();
      resultEl.textContent = `${data.status} video=${data.video_frames} ${data.last_result || ""}`;
      document.getElementById("available").textContent = (data.available_commands || []).join(", ");
    }
    setInterval(refreshStatus, 1000);
    refreshStatus();
  </script>
</body>
</html>
"""


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Simple Go2 video/control browser GUI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await ControlGui(args.host, args.port).run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
