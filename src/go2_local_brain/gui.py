"""Unified browser GUI for manual control, AI commands, video, and LiDAR."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from aiohttp import web

from .brain.local_llm import LocalRobotBrain
from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient
from .viewer import _jpeg_from_frame, _lidar_payload_from_message

log = logging.getLogger(__name__)

_LIDAR_SWITCH_TOPIC = "rt/utlidar/switch"
_LIDAR_ARRAY_TOPIC = "rt/utlidar/voxel_map_compressed"
_MAX_LIDAR_POINTS = 6000


class UnifiedGui:
    """One WebRTC session shared by manual controls, AI tools, and live sensors."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._client: Go2WebRTCClient | None = None
        self._brain: LocalRobotBrain | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._state_changed = asyncio.Condition()
        self._latest_jpeg: bytes | None = None
        self._latest_video_ts = 0.0
        self._latest_lidar: dict[str, Any] | None = None
        self._video_frames = 0
        self._lidar_messages = 0
        self._status = "starting"
        self._last_result = ""

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        cfg = load_config()
        self._client = Go2WebRTCClient(
            Go2Config(
                ip=cfg.go2_ip,
                aes_128_key=cfg.go2_aes_128_key,
                force_motion_mode=cfg.force_motion_mode,
                enable_exploration=cfg.enable_exploration,
                exploration_min_obstacle_m=cfg.exploration_min_obstacle_m,
                exploration_mode=cfg.exploration_mode,
                exploration_max_duration_s=cfg.exploration_max_duration_s,
            )
        )

        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._websocket)
        app.router.add_get("/video.mjpg", self._video_stream)
        app.router.add_get("/status.json", self._status_json)
        app.router.add_post("/api/manual/{action}", self._manual_action)
        app.router.add_post("/api/ai", self._ai_action)
        app.router.add_post("/api/stop", self._stop_action)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("unified GUI listening on http://%s:%s", self._host, self._port)

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
        self._brain = LocalRobotBrain(self._client, model=model)
        self._status = "connected"
        self._attach_sensor_streams()
        await self._broadcast_status()

    async def _shutdown(self) -> None:
        if self._client is None:
            return
        try:
            conn = getattr(self._client, "_conn", None)
            datachannel = getattr(conn, "datachannel", None)
            pubsub = getattr(datachannel, "pub_sub", None) if datachannel is not None else None
            if pubsub is not None:
                pubsub.publish_without_callback(_LIDAR_SWITCH_TOPIC, "off")
        except Exception as exc:  # noqa: BLE001
            log.debug("lidar switch off failed: %s", exc)
        await self._client.close()

    def _attach_sensor_streams(self) -> None:
        assert self._client is not None
        conn = getattr(self._client, "_conn", None)
        datachannel = getattr(conn, "datachannel", None)
        if datachannel is not None:
            disable_traffic_saving = getattr(datachannel, "disableTrafficSaving", None)
            if callable(disable_traffic_saving):
                result = disable_traffic_saving(True)
                if asyncio.iscoroutine(result) and self._loop is not None:
                    self._loop.create_task(result)
            set_decoder = getattr(datachannel, "set_decoder", None)
            if callable(set_decoder):
                set_decoder(decoder_type="libvoxel")

            pubsub = getattr(datachannel, "pub_sub", None)
            if pubsub is not None:
                pubsub.publish_without_callback(_LIDAR_SWITCH_TOPIC, "on")
                pubsub.subscribe(_LIDAR_ARRAY_TOPIC, self._on_lidar_message)

        video = getattr(conn, "video", None)
        if video is not None:
            video.switchVideoChannel(True)
            video.add_track_callback(self._recv_video_track)

    def _on_lidar_message(self, message: Any) -> None:
        payload = _lidar_payload_from_message(message, max_points=_MAX_LIDAR_POINTS)
        if payload is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._set_lidar(payload)))

    async def _set_lidar(self, payload: dict[str, Any]) -> None:
        self._latest_lidar = payload
        self._lidar_messages += 1
        await self._broadcast_json({"type": "lidar", **payload})
        await self._broadcast_status()

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

    async def _manual_action(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        payload = await _json_or_empty(request)
        try:
            result = await self._run_manual_action(action, payload)
        except Exception as exc:  # noqa: BLE001
            log.exception("manual action failed: %s", action)
            result = f"{action} failed: {exc}"
            await self._safe_stop()
            return web.json_response({"ok": False, "result": result}, status=400)
        self._last_result = result
        await self._broadcast_status()
        return web.json_response({"ok": True, "result": result})

    async def _ai_action(self, request: web.Request) -> web.Response:
        if self._brain is None:
            return web.json_response({"ok": False, "result": "AI brain is not connected"}, status=503)
        payload = await _json_or_empty(request)
        prompt = str(payload.get("prompt", "")).strip()
        if not prompt:
            return web.json_response({"ok": False, "result": "prompt is required"}, status=400)
        result = await self._brain.handle(prompt)
        self._last_result = result
        await self._broadcast_status()
        return web.json_response({"ok": True, "result": result})

    async def _stop_action(self, _request: web.Request) -> web.Response:
        await self._safe_stop()
        self._last_result = "stop"
        await self._broadcast_status()
        return web.json_response({"ok": True, "result": "stop"})

    async def _run_manual_action(self, action: str, payload: dict[str, Any]) -> str:
        if self._client is None:
            raise RuntimeError("client is not connected")
        speed = float(payload.get("speed", 0.45))
        turn = float(payload.get("turn", 0.8))
        duration = float(payload.get("duration_s", 0.35))
        actions: dict[str, Callable[[], Awaitable[None]]] = {
            "forward": lambda: self._client.move(speed, 0.0, 0.0, duration),
            "back": lambda: self._client.move(-speed * 0.75, 0.0, 0.0, duration),
            "left": lambda: self._client.move(0.0, speed * 0.6, 0.0, duration),
            "right": lambda: self._client.move(0.0, -speed * 0.6, 0.0, duration),
            "turn_left": lambda: self._client.move(0.0, 0.0, turn, duration),
            "turn_right": lambda: self._client.move(0.0, 0.0, -turn, duration),
            "walk_turn_left": lambda: self._client.move(speed * 0.75, 0.0, turn * 0.75, duration),
            "walk_turn_right": lambda: self._client.move(speed * 0.75, 0.0, -turn * 0.75, duration),
            "stand": self._client.stand_up,
            "balance": self._client.balance_stand,
            "sit": self._client.sit_down,
            "recovery": self._client.recovery_stand,
            "greet": lambda: self._client.advanced_action("greet"),
            "dance": lambda: self._client.dance_move("hype"),
            "jump": lambda: self._client.advanced_action("jump"),
            "pounce": lambda: self._client.advanced_action("pounce"),
            "handstand": lambda: self._client.advanced_action("handstand"),
            "backstand": lambda: self._client.advanced_action("backstand"),
            "turn_180_left": lambda: self._client.turn_180("left"),
            "turn_180_right": lambda: self._client.turn_180("right"),
        }
        fn = actions.get(action)
        if fn is None:
            raise RuntimeError(f"unknown manual action {action!r}")
        await fn()
        return action

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
            "lidar_messages": self._lidar_messages,
            "last_result": self._last_result,
        }


async def _json_or_empty(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified Go2 browser controller")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    await UnifiedGui(args.host, args.port).run()


def main() -> None:
    asyncio.run(_amain())


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Go2 Unified Control</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; background: #101113; color: #e8e8e8; font-family: system-ui, Segoe UI, sans-serif; }
    header { height: 46px; display: flex; align-items: center; justify-content: space-between; padding: 0 14px; background: #1b1d21; border-bottom: 1px solid #333841; }
    main { height: calc(100vh - 46px); display: grid; grid-template-columns: minmax(330px, 380px) 1fr; }
    aside { padding: 12px; overflow: auto; border-right: 1px solid #333841; background: #17191d; }
    section { min-width: 0; min-height: 0; }
    .media { display: grid; grid-template-rows: minmax(220px, 45%) 1fr; height: 100%; }
    .videoWrap { background: #050505; display: flex; align-items: center; justify-content: center; border-bottom: 1px solid #333841; }
    #video { width: 100%; height: 100%; object-fit: contain; }
    #lidarPanel { position: relative; min-height: 0; }
    #lidarCanvas { width: 100%; height: 100%; display: block; }
    h2 { font-size: 14px; margin: 16px 0 8px; color: #aeb7c2; font-weight: 650; }
    button, input, textarea { font: inherit; }
    button { border: 1px solid #3c4652; background: #242a31; color: #f1f1f1; border-radius: 6px; padding: 9px 10px; cursor: pointer; }
    button:hover { background: #303843; }
    .stop { background: #8c1d2c; border-color: #b72b3d; }
    .grid { display: grid; gap: 8px; }
    .drive { grid-template-columns: repeat(3, 1fr); }
    .actions { grid-template-columns: repeat(2, 1fr); }
    .wide { grid-column: 1 / -1; }
    textarea { width: 100%; min-height: 72px; resize: vertical; border: 1px solid #3c4652; background: #0e1012; color: #fff; border-radius: 6px; padding: 8px; }
    label { display: grid; gap: 5px; font-size: 12px; color: #b6bec8; }
    input[type=range] { width: 100%; }
    #result, #hud { margin-top: 10px; padding: 8px; background: #0e1012; border: 1px solid #333841; border-radius: 6px; min-height: 38px; font-size: 13px; color: #cbd5df; }
    #hud { position: absolute; left: 12px; bottom: 12px; margin: 0; background: rgba(0,0,0,.68); }
    @media (max-width: 900px) { main { grid-template-columns: 1fr; grid-template-rows: auto 1fr; } aside { border-right: 0; border-bottom: 1px solid #333841; } }
  </style>
</head>
<body>
  <header><strong>Go2 Unified Control</strong><span id="status">starting</span></header>
  <main>
    <aside>
      <button class="stop wide" onclick="stopNow()">STOP</button>
      <h2>Drive</h2>
      <div class="grid drive">
        <span></span><button onmousedown="hold('forward')" onmouseup="stopNow()" ontouchstart="hold('forward')" ontouchend="stopNow()">Forward</button><span></span>
        <button onmousedown="hold('left')" onmouseup="stopNow()" ontouchstart="hold('left')" ontouchend="stopNow()">Left</button>
        <button onclick="manual('balance')">Balance</button>
        <button onmousedown="hold('right')" onmouseup="stopNow()" ontouchstart="hold('right')" ontouchend="stopNow()">Right</button>
        <button onclick="manual('turn_left')">Turn L</button><button onmousedown="hold('back')" onmouseup="stopNow()" ontouchstart="hold('back')" ontouchend="stopNow()">Back</button><button onclick="manual('turn_right')">Turn R</button>
      </div>
      <h2>Speed</h2>
      <label>Move speed <input id="speed" type="range" min="0.1" max="1.0" value="0.45" step="0.05"></label>
      <label>Turn speed <input id="turn" type="range" min="0.2" max="1.5" value="0.8" step="0.05"></label>
      <h2>Posture And Actions</h2>
      <div class="grid actions">
        <button onclick="manual('stand')">Stand</button><button onclick="manual('sit')">Sit</button>
        <button onclick="manual('recovery')">Recovery</button><button onclick="manual('greet')">Greet</button>
        <button onclick="manual('dance')">Dance</button><button onclick="manual('jump')">Jump</button>
        <button onclick="manual('pounce')">Pounce</button><button onclick="manual('turn_180_left')">180 L</button>
        <button onclick="manual('handstand')">Handstand</button><button onclick="manual('backstand')">Backstand</button>
      </div>
      <h2>AI Command</h2>
      <textarea id="prompt" placeholder="turn right 90 degrees, then walk forward"></textarea>
      <button class="wide" onclick="sendAi()">Run AI Command</button>
      <div id="result"></div>
    </aside>
    <section class="media">
      <div class="videoWrap"><img id="video" src="/video.mjpg" alt="Live robot video"></div>
      <div id="lidarPanel"><canvas id="lidarCanvas"></canvas><div id="hud">LiDAR: <span id="lidarCount">0</span></div></div>
    </section>
  </main>
  <script>
    const speed = () => Number(document.getElementById("speed").value);
    const turn = () => Number(document.getElementById("turn").value);
    let holdTimer = null;
    async function api(path, body = {}) {
      const res = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
      const data = await res.json();
      document.getElementById("result").textContent = data.result || "";
      return data;
    }
    function manual(action) { return api(`/api/manual/${action}`, {speed:speed(), turn:turn(), duration_s:0.35}); }
    function hold(action) {
      manual(action);
      clearInterval(holdTimer);
      holdTimer = setInterval(() => manual(action), 280);
    }
    function stopNow() { clearInterval(holdTimer); holdTimer = null; return api("/api/stop"); }
    function sendAi() { const prompt = document.getElementById("prompt").value; return api("/api/ai", {prompt}); }
    document.addEventListener("keydown", (e) => {
      if (e.repeat) return;
      if (e.key === " ") stopNow();
      if (e.key === "w") hold("forward");
      if (e.key === "s") hold("back");
      if (e.key === "a") hold("left");
      if (e.key === "d") hold("right");
      if (e.key === "q") hold("turn_left");
      if (e.key === "e") hold("turn_right");
    });
    document.addEventListener("keyup", (e) => { if ("wasdqe".includes(e.key)) stopNow(); });

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
    function resize() {
      const r = canvas.parentElement.getBoundingClientRect();
      renderer.setSize(r.width, r.height, false);
      camera.aspect = r.width / Math.max(1, r.height);
      camera.updateProjectionMatrix();
    }
    window.addEventListener("resize", resize); resize();
    function setCloud(points, distances) {
      if (cloud) scene.remove(cloud);
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(points.flat(), 3));
      const max = Math.max(...distances, 1), colors = [];
      for (const d of distances) { const c = new THREE.Color(); c.setHSL(Math.min(d / max, 1) * 0.65, 1, 0.55); colors.push(c.r, c.g, c.b); }
      geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
      cloud = new THREE.Points(geo, new THREE.PointsMaterial({size:0.035, vertexColors:true}));
      scene.add(cloud);
    }
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "status") {
        document.getElementById("status").textContent = `${msg.status} video=${msg.video_frames} lidar=${msg.lidar_messages}`;
        if (msg.last_result) document.getElementById("result").textContent = msg.last_result;
      }
      if (msg.type === "lidar") {
        document.getElementById("lidarCount").textContent = `${msg.point_count} / ${msg.source_point_count}`;
        setCloud(msg.points, msg.distances);
      }
    };
    function animate() { requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera); }
    animate();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
