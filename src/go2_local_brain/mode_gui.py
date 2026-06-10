"""Configurable browser GUIs for video, AI, keyboard control, and LiDAR modes."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient
from .viewer import _jpeg_from_frame, _lidar_payload_from_message

log = logging.getLogger(__name__)

_LIDAR_SWITCH_TOPIC = "rt/utlidar/switch"
_LIDAR_TOPIC = "rt/utlidar/voxel_map"
_LIDAR_ARRAY_TOPIC = "rt/utlidar/voxel_map_compressed"
_MAX_LIDAR_POINTS = 1200
_LIDAR_SEND_PERIOD_S = 0.25
_MOVE_DURATION_S = 0.32
_MODE_SETTLE_S = 0.25

_MOTION_MODE_COMMANDS: dict[str, list[tuple[str, dict[str, Any] | None]]] = {
    "normal": [
        ("HandStand", {"data": False}),
        ("FreeBound", {"data": False}),
        ("FreeJump", {"data": False}),
        ("FreeAvoid", {"data": False}),
        ("WalkUpright", {"data": False}),
        ("CrossStep", {"data": False}),
        ("ClassicWalk", {"data": False}),
        ("BalanceStand", None),
    ],
    "hind_walk": [
        ("HandStand", {"data": False}),
        ("FreeBound", {"data": False}),
        ("FreeJump", {"data": False}),
        ("CrossStep", {"data": False}),
        ("WalkUpright", {"data": True}),
    ],
    "backstand": [
        ("HandStand", {"data": False}),
        ("WalkUpright", {"data": True}),
        ("BackStand", {"data": True}),
    ],
    "handstand": [
        ("WalkUpright", {"data": False}),
        ("FreeBound", {"data": False}),
        ("FreeJump", {"data": False}),
        ("HandStand", {"data": True}),
    ],
    "bound": [
        ("HandStand", {"data": False}),
        ("WalkUpright", {"data": False}),
        ("FreeJump", {"data": False}),
        ("FreeBound", {"data": True}),
    ],
    "jump": [
        ("HandStand", {"data": False}),
        ("WalkUpright", {"data": False}),
        ("FreeBound", {"data": False}),
        ("FreeJump", {"data": True}),
    ],
    "classic": [
        ("WalkUpright", {"data": False}),
        ("ClassicWalk", {"data": True}),
    ],
    "cross_step": [
        ("WalkUpright", {"data": False}),
        ("CrossStep", {"data": True}),
    ],
    "free_walk": [("FreeWalk", None)],
    "static_walk": [("StaticWalk", None)],
    "trot_run": [("TrotRun", None)],
    "economic": [("EconomicGait", None)],
}


@dataclass(frozen=True)
class GuiMode:
    """Feature switches for one browser control mode."""

    title: str
    enable_ai: bool
    enable_keyboard: bool
    enable_lidar: bool
    show_drive_panel: bool = False


class ModeGui:
    """One WebRTC connection with an intentionally narrow feature set."""

    def __init__(self, host: str, port: int, mode: GuiMode) -> None:
        self._host = host
        self._port = port
        self._mode = mode
        self._client: Go2WebRTCClient | None = None
        self._brain: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._state_changed = asyncio.Condition()
        self._latest_jpeg: bytes | None = None
        self._latest_video_ts = 0.0
        self._latest_lidar: dict[str, Any] | None = None
        self._video_frames = 0
        self._lidar_raw_messages = 0
        self._lidar_messages = 0
        self._lidar_parse_errors = 0
        self._lidar_dropped = 0
        self._last_lidar_send_ts = 0.0
        self._last_lidar_error = ""
        self._status = "starting"
        self._last_result = ""
        self._active_motion_mode = "normal"

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
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
                enable_exploration=cfg.enable_exploration,
                exploration_min_obstacle_m=cfg.exploration_min_obstacle_m,
                exploration_mode=cfg.exploration_mode,
                exploration_max_duration_s=cfg.exploration_max_duration_s,
            )
        )

        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get("/", self._index)
        app.router.add_get("/video.mjpg", self._video_stream)
        app.router.add_get("/status.json", self._status_json)
        app.router.add_get("/ws", self._websocket)
        app.router.add_post("/api/stop", self._stop_action)
        if self._mode.enable_ai:
            app.router.add_post("/api/ai", self._ai_action)
        if self._mode.enable_keyboard:
            app.router.add_post("/api/move", self._move_action)
            app.router.add_post("/api/mode", self._mode_action)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("%s listening on http://%s:%s", self._mode.title, self._host, self._port)

        try:
            await self._connect(cfg.ollama_model)
            while True:
                await asyncio.sleep(3600)
        finally:
            await self._shutdown()
            await runner.cleanup()

    async def _connect(self, model: str) -> None:
        assert self._client is not None
        self._status = "connecting"
        await self._broadcast_status()
        await self._client.connect()
        if self._mode.enable_ai:
            from .brain.local_llm import LocalRobotBrain

            self._brain = LocalRobotBrain(self._client, model=model)
        self._attach_video()
        if self._mode.enable_lidar:
            self._attach_lidar()
        self._status = "connected"
        await self._broadcast_status()

    async def _shutdown(self) -> None:
        if self._client is None:
            return
        if self._mode.enable_lidar:
            try:
                conn = getattr(self._client, "_conn", None)
                datachannel = getattr(conn, "datachannel", None)
                pubsub = getattr(datachannel, "pub_sub", None) if datachannel is not None else None
                if pubsub is not None:
                    pubsub.publish_without_callback(_LIDAR_SWITCH_TOPIC, "off")
            except Exception as exc:  # noqa: BLE001
                log.debug("lidar switch off failed: %s", exc)
        await self._client.close()

    def _attach_video(self) -> None:
        assert self._client is not None
        conn = getattr(self._client, "_conn", None)
        video = getattr(conn, "video", None)
        if video is None:
            raise RuntimeError("WebRTC video interface not found")
        video.switchVideoChannel(True)
        video.add_track_callback(self._recv_video_track)

    def _attach_lidar(self) -> None:
        assert self._client is not None
        conn = getattr(self._client, "_conn", None)
        datachannel = getattr(conn, "datachannel", None)
        if datachannel is None:
            raise RuntimeError("WebRTC data channel not found")
        disable_traffic_saving = getattr(datachannel, "disableTrafficSaving", None)
        if callable(disable_traffic_saving):
            result = disable_traffic_saving(True)
            if asyncio.iscoroutine(result) and self._loop is not None:
                self._loop.create_task(result)
        set_decoder = getattr(datachannel, "set_decoder", None)
        if callable(set_decoder):
            set_decoder(decoder_type="libvoxel")
        pubsub = getattr(datachannel, "pub_sub", None)
        if pubsub is None:
            raise RuntimeError("WebRTC pub/sub interface not found")
        pubsub.publish_without_callback(_LIDAR_SWITCH_TOPIC, "on")
        pubsub.subscribe(_LIDAR_TOPIC, self._on_lidar_message)
        pubsub.subscribe(_LIDAR_ARRAY_TOPIC, self._on_lidar_message)

    async def _recv_video_track(self, track: Any) -> None:
        while True:
            frame = await track.recv()
            jpeg = _jpeg_from_frame(frame)
            async with self._state_changed:
                self._latest_jpeg = jpeg
                self._latest_video_ts = time.time()
                self._video_frames += 1
                self._state_changed.notify_all()

    def _on_lidar_message(self, message: Any) -> None:
        self._lidar_raw_messages += 1
        payload = _lidar_payload_from_message(message, max_points=_MAX_LIDAR_POINTS)
        if payload is None:
            self._lidar_parse_errors += 1
            if self._lidar_parse_errors <= 5:
                self._last_lidar_error = _summarize_lidar_message(message)
                log.warning("could not parse lidar message shape: %s", self._last_lidar_error)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._broadcast_status()))
            return
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._set_lidar(payload)))

    async def _set_lidar(self, payload: dict[str, Any]) -> None:
        now = time.monotonic()
        if now - self._last_lidar_send_ts < _LIDAR_SEND_PERIOD_S:
            self._lidar_dropped += 1
            return
        self._last_lidar_send_ts = now
        self._latest_lidar = payload
        self._lidar_messages += 1
        await self._broadcast_json({"type": "lidar", **payload})
        await self._broadcast_status()

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(text=_html_for_mode(self._mode), content_type="text/html")

    async def _status_json(self, _request: web.Request) -> web.Response:
        return web.json_response(self._status_payload())

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        await ws.send_str(json.dumps({"type": "status", **self._status_payload()}))
        if self._latest_lidar is not None:
            await ws.send_str(json.dumps({"type": "lidar", **self._latest_lidar}))
        try:
            async for _message in ws:
                pass
        finally:
            self._ws_clients.discard(ws)
        return ws

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
            if jpeg is not None:
                await response.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                await response.write(jpeg)
                await response.write(b"\r\n")

    async def _ai_action(self, request: web.Request) -> web.Response:
        if self._brain is None:
            return web.json_response({"ok": False, "result": "AI brain is not connected"}, status=503)
        payload = await _json_or_empty(request)
        prompt = str(payload.get("prompt", "")).strip()
        if not prompt:
            return web.json_response({"ok": False, "result": "prompt is required"}, status=400)
        try:
            result = await self._brain.handle(prompt)
        except Exception as exc:  # noqa: BLE001
            log.exception("AI command failed")
            await self._safe_stop()
            return web.json_response({"ok": False, "result": f"AI command failed: {exc}"}, status=400)
        self._last_result = result
        await self._broadcast_status()
        return web.json_response({"ok": True, "result": result})

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
        await self._broadcast_status()
        return web.json_response({"ok": True, "result": result})

    async def _mode_action(self, request: web.Request) -> web.Response:
        if self._client is None:
            return web.json_response({"ok": False, "result": "client is not connected"}, status=503)
        payload = await _json_or_empty(request)
        mode = str(payload.get("mode", "")).strip()
        commands = _MOTION_MODE_COMMANDS.get(mode)
        if commands is None:
            return web.json_response({"ok": False, "result": f"unknown mode {mode!r}"}, status=400)
        try:
            await self._client.stop()
            for name, parameter in commands:
                await self._client.sport_command(name, parameter)
                await asyncio.sleep(_MODE_SETTLE_S)
        except Exception as exc:  # noqa: BLE001
            log.exception("motion mode failed: %s", mode)
            await self._safe_stop()
            return web.json_response({"ok": False, "result": f"{mode} failed at {name}: {exc}"}, status=400)
        self._active_motion_mode = mode
        self._last_result = f"mode: {mode}"
        await self._broadcast_status()
        return web.json_response({"ok": True, "result": self._last_result})

    async def _stop_action(self, _request: web.Request) -> web.Response:
        await self._safe_stop()
        self._last_result = "stop"
        await self._broadcast_status()
        return web.json_response({"ok": True, "result": "stop"})

    async def _safe_stop(self) -> None:
        if self._client is not None:
            await self._client.stop()

    async def _broadcast_status(self) -> None:
        await self._broadcast_json({"type": "status", **self._status_payload()})

    async def _broadcast_json(self, payload: dict[str, Any]) -> None:
        if not self._ws_clients:
            return
        text = json.dumps(payload, separators=(",", ":"))
        stale: list[web.WebSocketResponse] = []
        for ws in self._ws_clients:
            try:
                await ws.send_str(text)
            except Exception:  # noqa: BLE001
                stale.append(ws)
        for ws in stale:
            self._ws_clients.discard(ws)

    def _status_payload(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "video_frames": self._video_frames,
            "lidar_raw_messages": self._lidar_raw_messages,
            "lidar_messages": self._lidar_messages,
            "lidar_parse_errors": self._lidar_parse_errors,
            "lidar_dropped": self._lidar_dropped,
            "last_lidar_error": self._last_lidar_error,
            "last_result": self._last_result,
            "motion_mode": self._active_motion_mode,
        }


async def _json_or_empty(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _summarize_lidar_message(message: Any) -> str:
    if not isinstance(message, dict):
        return type(message).__name__
    data = message.get("data")
    if not isinstance(data, dict):
        return f"data={type(data).__name__}"
    nested = data.get("data")
    keys = sorted(str(k) for k in data.keys())
    if isinstance(nested, dict):
        return f"data_keys={keys}; nested_keys={sorted(str(k) for k in nested.keys())}"
    return f"data_keys={keys}; nested={type(nested).__name__}"


def _html_for_mode(mode: GuiMode) -> str:
    ai_panel = _AI_PANEL if mode.enable_ai else ""
    drive_panel = _DRIVE_PANEL if mode.show_drive_panel else ""
    keyboard_hint = _KEYBOARD_HIDDEN_INPUTS if mode.enable_keyboard and not mode.show_drive_panel else ""
    media_class = "media lidar-on" if mode.enable_lidar else "media"
    lidar_panel = _LIDAR_PANEL if mode.enable_lidar else ""
    three_scripts = _THREE_SCRIPTS if mode.enable_lidar else ""
    lidar_js = _LIDAR_JS if mode.enable_lidar else ""
    keyboard_js = _KEYBOARD_JS if mode.enable_keyboard else ""
    ai_js = _AI_JS if mode.enable_ai else ""
    return _HTML_TEMPLATE.replace("__TITLE__", mode.title).replace("__AI_PANEL__", ai_panel).replace(
        "__DRIVE_PANEL__", drive_panel
    ).replace("__KEYBOARD_HINT__", keyboard_hint).replace("__MEDIA_CLASS__", media_class).replace(
        "__LIDAR_PANEL__", lidar_panel
    ).replace("__THREE_SCRIPTS__", three_scripts).replace("__KEYBOARD_JS__", keyboard_js).replace(
        "__AI_JS__", ai_js
    ).replace("__LIDAR_JS__", lidar_js)


def make_main(mode: GuiMode, default_port: int) -> None:
    parser = argparse.ArgumentParser(description=mode.title)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(ModeGui(args.host, args.port, mode).run())


_THREE_SCRIPTS = """
  <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
"""

_AI_PANEL = """
      <h2>AI CLI</h2>
      <textarea id="prompt" placeholder="turn right 90 degrees, then walk forward"></textarea>
      <button class="wide" onclick="sendAi()">Run AI Command</button>
"""

_DRIVE_PANEL = """
      <h2>Drive</h2>
      <div class="grid drive">
        <span></span><button data-move="forward">Forward</button><span></span>
        <button data-move="left">Left</button><button onclick="stopNow()">Stop</button><button data-move="right">Right</button>
        <button data-move="turnLeft">Turn L</button><button data-move="back">Back</button><button data-move="turnRight">Turn R</button>
        <button data-move="walkTurnLeft">Walk+L</button><button onclick="stopNow()">STOP</button><button data-move="walkTurnRight">Walk+R</button>
      </div>
      <label>Speed <input id="speed" type="range" min="0.10" max="0.75" step="0.05" value="0.45"></label>
      <label>Turn <input id="turn" type="range" min="0.20" max="1.10" step="0.05" value="0.85"></label>
      <h2>Locomotion Mode</h2>
      <div class="grid modegrid">
        <button data-mode="normal">Normal</button>
        <button data-mode="hind_walk">Hind Walk</button>
        <button data-mode="backstand">BackStand</button>
        <button data-mode="handstand">HandStand</button>
        <button data-mode="bound">Bound</button>
        <button data-mode="jump">Jump</button>
        <button data-mode="classic">Classic</button>
        <button data-mode="cross_step">CrossStep</button>
        <button data-mode="free_walk">FreeWalk</button>
        <button data-mode="static_walk">StaticWalk</button>
        <button data-mode="trot_run">TrotRun</button>
        <button data-mode="economic">Economic</button>
      </div>
"""

_KEYBOARD_HIDDEN_INPUTS = """
      <input id="speed" type="hidden" value="0.45">
      <input id="turn" type="hidden" value="0.85">
"""

_LIDAR_PANEL = """
      <div id="lidarPanel"><canvas id="lidarCanvas"></canvas><div id="hud">LiDAR: <span id="lidarCount">0</span><br><span id="lidarDebug">waiting</span><br><span id="lidarBounds">bounds: none</span></div></div>
"""

_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
__THREE_SCRIPTS__
  <style>
    :root { color-scheme: dark; --bg:#101113; --panel:#17191d; --line:#333841; --text:#e8e8e8; --muted:#aeb7c2; --danger:#8c1d2c; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:system-ui, Segoe UI, sans-serif; }
    header { height:46px; display:flex; align-items:center; justify-content:space-between; padding:0 14px; background:#1b1d21; border-bottom:1px solid var(--line); }
    main { height:calc(100vh - 46px); display:grid; grid-template-columns:minmax(330px, 390px) 1fr; }
    aside { padding:12px; overflow:auto; border-right:1px solid var(--line); background:var(--panel); }
    .media { min-width:0; min-height:0; height:100%; display:grid; grid-template-rows:1fr; }
    .media.lidar-on { grid-template-rows:minmax(220px, 45%) 1fr; }
    .videoWrap { background:#050505; display:flex; align-items:center; justify-content:center; border-bottom:1px solid var(--line); min-height:0; }
    #video { width:100%; height:100%; object-fit:contain; display:block; }
    #lidarPanel { position:relative; min-height:0; }
    #lidarCanvas { width:100%; height:100%; display:block; }
    h2 { font-size:14px; margin:16px 0 8px; color:var(--muted); font-weight:650; }
    button, input, textarea { font:inherit; }
    button { border:1px solid #3c4652; background:#242a31; color:#f1f1f1; border-radius:6px; padding:9px 10px; cursor:pointer; min-height:36px; }
    button:hover { background:#303843; }
    button.active { border-color:#78b7ff; background:#19354f; }
    .stop { background:var(--danger); border-color:#b72b3d; }
    .grid { display:grid; gap:8px; }
    .drive { grid-template-columns:repeat(3, 1fr); }
    .modegrid { grid-template-columns:repeat(2, 1fr); }
    .wide { width:100%; margin-top:8px; }
    textarea { width:100%; min-height:76px; resize:vertical; border:1px solid #3c4652; background:#0e1012; color:#fff; border-radius:6px; padding:8px; }
    label { display:grid; gap:5px; font-size:12px; color:#b6bec8; margin-top:8px; }
    input[type=range] { width:100%; }
    #result, .hint, #hud { margin-top:10px; padding:8px; background:#0e1012; border:1px solid var(--line); border-radius:6px; min-height:38px; font-size:13px; color:#cbd5df; }
    #hud { position:absolute; left:12px; bottom:12px; margin:0; background:rgba(0,0,0,.68); }
    @media (max-width:900px) { main { grid-template-columns:1fr; grid-template-rows:auto 1fr; } aside { border-right:0; border-bottom:1px solid var(--line); } .media, .media.lidar-on { height:65vh; } }
  </style>
</head>
<body>
  <header><strong>__TITLE__</strong><span id="status">starting</span></header>
  <main>
    <aside>
      <button class="stop wide" onclick="stopNow()">STOP</button>
__AI_PANEL__
__DRIVE_PANEL__
__KEYBOARD_HINT__
      <div id="result"></div>
    </aside>
    <section class="__MEDIA_CLASS__">
      <div class="videoWrap"><img id="video" src="/video.mjpg" alt="Live robot video"></div>
__LIDAR_PANEL__
    </section>
  </main>
  <script>
    const resultEl = document.getElementById("result");
    async function api(path, body = {}) {
      const res = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
      const data = await res.json().catch(() => ({result:"bad response"}));
      resultEl.textContent = data.result || "";
      return data;
    }
    function stopNow() { active.clear(); clearInterval(tick); tick = null; return api("/api/stop"); }
    const active = new Set();
    let tick = null;
    function speed() { return Number(document.getElementById("speed")?.value || 0.45); }
    function turn() { return Number(document.getElementById("turn")?.value || 0.85); }
__AI_JS__
__KEYBOARD_JS__
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "status") {
        document.getElementById("status").textContent = `${msg.status} video=${msg.video_frames} lidar=${msg.lidar_messages} mode=${msg.motion_mode}`;
        if (msg.last_result) resultEl.textContent = msg.last_result;
        document.querySelectorAll("[data-mode]").forEach((button) => button.classList.toggle("active", button.dataset.mode === msg.motion_mode));
        const dbg = document.getElementById("lidarDebug");
        if (dbg) dbg.textContent = `raw=${msg.lidar_raw_messages} rendered=${msg.lidar_messages} parseErrors=${msg.lidar_parse_errors} dropped=${msg.lidar_dropped}`;
        if (msg.last_lidar_error) resultEl.textContent = `LiDAR parse shape: ${msg.last_lidar_error}`;
      }
      if (msg.type === "lidar" && window.setCloud) {
        document.getElementById("lidarCount").textContent = `${msg.point_count} / ${msg.source_point_count}`;
        if (msg.bounds) document.getElementById("lidarBounds").textContent = `bounds: min=${msg.bounds.min.map(v=>v.toFixed(1)).join(",")} max=${msg.bounds.max.map(v=>v.toFixed(1)).join(",")}`;
        window.setCloud(msg.points, msg.distances, msg.bounds);
      }
    };
__LIDAR_JS__
  </script>
</body>
</html>
"""

_AI_JS = """
    function sendAi() {
      const prompt = document.getElementById("prompt").value;
      return api("/api/ai", {prompt});
    }
"""

_KEYBOARD_JS = """
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
    document.querySelectorAll("[data-mode]").forEach((button) => {
      button.addEventListener("click", () => {
        active.clear();
        clearInterval(tick);
        tick = null;
        api("/api/mode", {mode: button.dataset.mode}).catch(() => {});
      });
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
"""

_LIDAR_JS = """
    const canvas = document.getElementById("lidarCanvas");
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x101113);
    const camera = new THREE.PerspectiveCamera(62, 1, 0.1, 1000);
    camera.position.set(4, 5, 7);
    const renderer = new THREE.WebGLRenderer({canvas, antialias:true});
    const controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    scene.add(new THREE.GridHelper(10, 20, 0x3a3f48, 0x242930));
    scene.add(new THREE.AxesHelper(1.5));
    let cloud = null;
    let cloudFitted = false;
    function resize() {
      const r = canvas.parentElement.getBoundingClientRect();
      renderer.setSize(r.width, r.height, false);
      camera.aspect = r.width / Math.max(1, r.height);
      camera.updateProjectionMatrix();
    }
    window.addEventListener("resize", resize); resize();
    function fitCloud(bounds) {
      if (!bounds || cloudFitted) return;
      const min = bounds.min, max = bounds.max;
      const center = new THREE.Vector3((min[0]+max[0])/2, (min[1]+max[1])/2, (min[2]+max[2])/2);
      const span = Math.max(max[0]-min[0], max[1]-min[1], max[2]-min[2], 1);
      controls.target.copy(center);
      camera.position.set(center.x + span * 1.4, center.y + span * 1.1, center.z + span * 1.4);
      camera.near = Math.max(0.01, span / 1000);
      camera.far = Math.max(1000, span * 20);
      camera.updateProjectionMatrix();
      controls.update();
      cloudFitted = true;
    }
    window.setCloud = function(points, distances, bounds) {
      if (!points || points.length === 0) return;
      if (cloud) scene.remove(cloud);
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(points.flat(), 3));
      const max = Math.max(...distances, 1), colors = [];
      for (const d of distances) { const c = new THREE.Color(); c.setHSL(Math.min(d / max, 1) * 0.65, 1, 0.55); colors.push(c.r, c.g, c.b); }
      geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
      cloud = new THREE.Points(geo, new THREE.PointsMaterial({size:0.035, vertexColors:true}));
      scene.add(cloud);
      fitCloud(bounds);
    };
    function animate() { requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera); }
    animate();
"""
