"""Browser viewer for live Go2 video and LiDAR."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from .config import load_config

log = logging.getLogger(__name__)

_LIDAR_SWITCH_TOPIC = "rt/utlidar/switch"
_LIDAR_TOPIC = "rt/utlidar/voxel_map"
_LIDAR_ARRAY_TOPIC = "rt/utlidar/voxel_map_compressed"
_MAX_LIDAR_POINTS = 1200
_JPEG_QUALITY = 75


@dataclass
class ViewerState:
    latest_jpeg: bytes | None = None
    latest_video_ts: float = 0.0
    latest_lidar: dict[str, Any] | None = None
    latest_lidar_ts: float = 0.0
    lidar_messages: int = 0
    video_frames: int = 0
    status: str = "starting"


class Go2BrowserViewer:
    """Serve live robot video and LiDAR over a small HTTP UI."""

    def __init__(self, robot_ip: str, host: str, port: int, *, lidar: bool = True, video: bool = True) -> None:
        self._robot_ip = robot_ip
        self._host = host
        self._port = port
        self._enable_lidar = lidar
        self._enable_video = video
        self._state = ViewerState()
        self._state_changed = asyncio.Condition()
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._conn: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        app = web.Application()
        app["viewer"] = self
        app.router.add_get("/", self._index)
        app.router.add_get("/video.mjpg", self._video_stream)
        app.router.add_get("/ws", self._websocket)
        app.router.add_get("/status.json", self._status)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("viewer listening on http://%s:%s", self._host, self._port)

        try:
            await self._connect_robot()
            while True:
                await asyncio.sleep(3600)
        finally:
            await self._close_robot()
            await runner.cleanup()

    async def _connect_robot(self) -> None:
        from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod  # type: ignore

        self._state.status = "connecting"
        await self._broadcast_status()
        self._conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip=self._robot_ip)
        await self._conn.connect()
        self._state.status = "connected"

        datachannel = getattr(self._conn, "datachannel", None)
        if datachannel is not None:
            disable_traffic_saving = getattr(datachannel, "disableTrafficSaving", None)
            if callable(disable_traffic_saving):
                result = disable_traffic_saving(True)
                if asyncio.iscoroutine(result):
                    await result
            set_decoder = getattr(datachannel, "set_decoder", None)
            if callable(set_decoder):
                set_decoder(decoder_type="libvoxel")

        if self._enable_lidar:
            self._start_lidar()
        if self._enable_video:
            self._start_video()
        await self._broadcast_status()

    async def _close_robot(self) -> None:
        if self._conn is None:
            return
        try:
            datachannel = getattr(self._conn, "datachannel", None)
            pubsub = getattr(datachannel, "pub_sub", None) if datachannel is not None else None
            if pubsub is not None:
                pubsub.publish_without_callback(_LIDAR_SWITCH_TOPIC, "off")
        except Exception as exc:  # noqa: BLE001
            log.debug("lidar switch off failed: %s", exc)
        try:
            await self._conn.disconnect()
        except Exception as exc:  # noqa: BLE001
            log.warning("viewer disconnect failed: %s", exc)
        self._conn = None

    def _start_lidar(self) -> None:
        datachannel = getattr(self._conn, "datachannel", None)
        pubsub = getattr(datachannel, "pub_sub", None) if datachannel is not None else None
        if pubsub is None:
            raise RuntimeError("WebRTC data channel pub/sub interface not found")
        pubsub.publish_without_callback(_LIDAR_SWITCH_TOPIC, "on")
        pubsub.subscribe(_LIDAR_TOPIC, self._on_lidar_message)
        pubsub.subscribe(_LIDAR_ARRAY_TOPIC, self._on_lidar_message)
        log.info("lidar stream subscribed")

    def _start_video(self) -> None:
        video = getattr(self._conn, "video", None)
        if video is None:
            raise RuntimeError("WebRTC video interface not found")
        video.switchVideoChannel(True)
        video.add_track_callback(self._recv_video_track)
        log.info("video stream enabled")

    def _on_lidar_message(self, message: Any) -> None:
        payload = _lidar_payload_from_message(message, max_points=_MAX_LIDAR_POINTS)
        if payload is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._set_lidar(payload)))

    async def _set_lidar(self, payload: dict[str, Any]) -> None:
        self._state.latest_lidar = payload
        self._state.latest_lidar_ts = time.time()
        self._state.lidar_messages += 1
        await self._broadcast_json({"type": "lidar", **payload})
        await self._broadcast_status()

    async def _recv_video_track(self, track: Any) -> None:
        while True:
            frame = await track.recv()
            jpeg = _jpeg_from_frame(frame)
            async with self._state_changed:
                self._state.latest_jpeg = jpeg
                self._state.latest_video_ts = time.time()
                self._state.video_frames += 1
                self._state_changed.notify_all()

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
        now = time.time()
        video_age = None if not self._state.latest_video_ts else now - self._state.latest_video_ts
        lidar_age = None if not self._state.latest_lidar_ts else now - self._state.latest_lidar_ts
        return {
            "robot_ip": self._robot_ip,
            "status": self._state.status,
            "video_frames": self._state.video_frames,
            "lidar_messages": self._state.lidar_messages,
            "video_age_s": video_age,
            "lidar_age_s": lidar_age,
        }

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _status(self, _request: web.Request) -> web.Response:
        return web.json_response(self._status_payload())

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        await ws.send_str(json.dumps({"type": "status", **self._status_payload()}))
        if self._state.latest_lidar is not None:
            await ws.send_str(json.dumps({"type": "lidar", **self._state.latest_lidar}))
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
                    lambda: self._state.latest_jpeg is not None and self._state.latest_video_ts != last_sent_ts
                )
                jpeg = self._state.latest_jpeg
                last_sent_ts = self._state.latest_video_ts
            if jpeg is None:
                continue
            await response.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
            await response.write(jpeg)
            await response.write(b"\r\n")


def _jpeg_from_frame(frame: Any) -> bytes:
    image = frame.to_image()
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return buf.getvalue()


def _lidar_payload_from_message(message: Any, *, max_points: int = _MAX_LIDAR_POINTS) -> dict[str, Any] | None:
    data, positions = _extract_lidar_positions(message)
    robot_points, source_point_count = _points_from_positions(positions)
    if not robot_points:
        return None
    points = _orient_points_for_three(robot_points)
    points = _decimate(points, max_points)
    robot_points = _decimate(robot_points, max_points)
    distances = [(x * x + y * y + z * z) ** 0.5 for x, y, z in points]
    bounds = _point_bounds(points)
    return {
        "points": points,
        "robot_points": robot_points,
        "distances": distances,
        "bounds": bounds,
        "point_count": len(points),
        "source_point_count": source_point_count,
        "stamp": data.get("stamp") if isinstance(data, dict) else None,
    }


def _extract_lidar_positions(message: Any) -> tuple[dict[str, Any], Any]:
    """Return decoded LiDAR metadata and flat xyz positions from known upstream shapes."""
    if not isinstance(message, dict):
        return {}, None
    data = message.get("data", {})
    if not isinstance(data, dict):
        return {}, None

    nested = data.get("data")
    if isinstance(nested, dict) and "positions" in nested:
        return data, nested.get("positions")
    if "positions" in data:
        return data, data.get("positions")
    return data, None


def _coerce_position_values(positions: Any) -> list[Any] | None:
    """Convert decoder outputs such as NumPy arrays into a plain sequence."""
    if positions is None:
        return None
    if isinstance(positions, list):
        return positions
    if isinstance(positions, tuple):
        return list(positions)
    if isinstance(positions, (bytes, bytearray)):
        return list(positions)

    tolist = getattr(positions, "tolist", None)
    if callable(tolist):
        values = tolist()
        if isinstance(values, list):
            return values
    try:
        return list(positions)
    except TypeError:
        return None


def _points_from_positions(positions: Any) -> tuple[list[list[float]], int]:
    values = _coerce_position_values(positions)
    if values is None or len(values) < 3:
        return [], 0
    points = _xyz_triplets(values)
    return points, len(values) // 3


def _orient_points_for_three(points: list[list[float]]) -> list[list[float]]:
    """Map robot xyz into Three.js coordinates and place the cloud on the grid."""
    if not points:
        return []
    oriented = [[-x, z, y] for x, y, z in points]
    center_x = sum(p[0] for p in oriented) / len(oriented)
    center_z = sum(p[2] for p in oriented) / len(oriented)
    min_y = min(p[1] for p in oriented)
    return [[x - center_x, y - min_y, z - center_z] for x, y, z in oriented]


def _point_bounds(points: list[list[float]]) -> dict[str, list[float]]:
    if not points:
        return {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    return {
        "min": [min(xs), min(ys), min(zs)],
        "max": [max(xs), max(ys), max(zs)],
    }


def _xyz_triplets(values: list[Any]) -> list[list[float]]:
    points: list[list[float]] = []
    usable_len = len(values) - (len(values) % 3)
    for i in range(0, usable_len, 3):
        try:
            points.append([float(values[i]), float(values[i + 1]), float(values[i + 2])])
        except (TypeError, ValueError):
            continue
    return points


def _decimate(points: list[list[float]], max_points: int) -> list[list[float]]:
    if max_points <= 0 or len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    return points[::step][:max_points]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve Go2 video and LiDAR in a browser")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-lidar", action="store_true")
    parser.add_argument("--no-video", action="store_true")
    return parser.parse_args()


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    cfg = load_config()
    viewer = Go2BrowserViewer(
        robot_ip=cfg.go2_ip,
        host=args.host,
        port=args.port,
        lidar=not args.no_lidar,
        video=not args.no_video,
    )
    await viewer.run()


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Go2 Live Viewer</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; font-family: system-ui, Segoe UI, sans-serif; background: #111; color: #eee; }
    header { height: 44px; display: flex; align-items: center; justify-content: space-between; padding: 0 14px; background: #1d1d1d; border-bottom: 1px solid #333; }
    main { display: grid; grid-template-columns: minmax(320px, 42vw) 1fr; height: calc(100vh - 44px); }
    #videoPanel { background: #050505; display: flex; align-items: center; justify-content: center; border-right: 1px solid #333; }
    #video { width: 100%; max-height: 100%; object-fit: contain; }
    #lidarPanel { position: relative; min-width: 0; }
    #lidarCanvas { width: 100%; height: 100%; display: block; }
    #hud { position: absolute; left: 12px; bottom: 12px; padding: 8px 10px; background: rgba(0,0,0,.65); border: 1px solid #444; border-radius: 6px; font-size: 13px; }
    .pill { color: #b7f7c8; }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; grid-template-rows: 40vh 1fr; }
      #videoPanel { border-right: 0; border-bottom: 1px solid #333; }
    }
  </style>
</head>
<body>
  <header>
    <strong>Go2 Live Viewer</strong>
    <span id="status">connecting</span>
  </header>
  <main>
    <section id="videoPanel"><img id="video" src="/video.mjpg" alt="Live robot video"></section>
    <section id="lidarPanel">
      <canvas id="lidarCanvas"></canvas>
      <div id="hud">LiDAR: <span id="lidarCount">0</span> pts<br>Status: <span class="pill" id="robotStatus">starting</span></div>
    </section>
  </main>
  <script>
    const canvas = document.getElementById("lidarCanvas");
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x111111);
    const camera = new THREE.PerspectiveCamera(62, 1, 0.1, 1000);
    camera.position.set(4, 5, 7);
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    const controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.target.set(0, 0, 0);
    controls.enableDamping = true;
    scene.add(new THREE.AxesHelper(1.5));
    scene.add(new THREE.GridHelper(10, 20, 0x444444, 0x222222));
    let cloud = null;

    function resize() {
      const rect = canvas.parentElement.getBoundingClientRect();
      renderer.setSize(rect.width, rect.height, false);
      camera.aspect = rect.width / Math.max(1, rect.height);
      camera.updateProjectionMatrix();
    }
    window.addEventListener("resize", resize);
    resize();

    function setCloud(points, distances) {
      if (cloud) scene.remove(cloud);
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(points.flat(), 3));
      const colors = [];
      const maxDist = Math.max(...distances, 1);
      for (const d of distances) {
        const color = new THREE.Color();
        color.setHSL(Math.min(d / maxDist, 1) * 0.65, 1, 0.55);
        colors.push(color.r, color.g, color.b);
      }
      geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
      cloud = new THREE.Points(geometry, new THREE.PointsMaterial({ size: 0.035, vertexColors: true }));
      scene.add(cloud);
    }

    const statusEl = document.getElementById("status");
    const robotStatusEl = document.getElementById("robotStatus");
    const lidarCountEl = document.getElementById("lidarCount");
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => { statusEl.textContent = "connected"; };
    ws.onclose = () => { statusEl.textContent = "disconnected"; };
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === "status") {
        robotStatusEl.textContent = `${msg.status} video=${msg.video_frames} lidar=${msg.lidar_messages}`;
      }
      if (msg.type === "lidar") {
        lidarCountEl.textContent = `${msg.point_count} / ${msg.source_point_count}`;
        setCloud(msg.points, msg.distances);
      }
    };

    function animate() {
      requestAnimationFrame(animate);
      controls.update();
      renderer.render(scene, camera);
    }
    animate();
  </script>
</body>
</html>
"""


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
