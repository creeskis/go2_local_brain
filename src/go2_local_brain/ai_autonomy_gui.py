"""AI-only autonomy mode: map patrol, perception hook, status, and video."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .autonomy.map import (
    PatrolMap,
    Waypoint,
    empty_patrol_map,
    list_patrol_maps,
    load_patrol_map,
    patrol_map_from_dict,
    save_patrol_map,
    safe_map_filename,
)
from .autonomy.navigator import AutonomyNavigator
from .autonomy.perception import (
    CameraOnlyPerceptionProvider,
    PerceptionHealth,
    PerceptionProvider,
    YoloPerceptionProvider,
)
from .autonomy.supervisor import AutonomySupervisor
from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient
from .viewer import _jpeg_from_frame

log = logging.getLogger(__name__)


class AiAutonomyGui:
    """Browser shell for activating and watching autonomous patrol."""

    def __init__(
        self,
        host: str,
        port: int,
        maps_dir: Path,
        map_path: Path | None,
        allow_no_detector: bool,
        detector: str,
        yolo_model: str,
        yolo_threshold: float,
        yolo_device: str,
    ) -> None:
        self._host = host
        self._port = port
        self._maps_dir = maps_dir
        self._map_path = map_path
        self._allow_no_detector = allow_no_detector
        self._detector = detector
        self._yolo_model = yolo_model
        self._yolo_threshold = yolo_threshold
        self._yolo_device = yolo_device or None
        self._client: Go2WebRTCClient | None = None
        self._supervisor: AutonomySupervisor | None = None
        self._patrol_map: PatrolMap | None = None
        self._perception: PerceptionProvider | None = None
        self._perception_health = PerceptionHealth(False, "not-started", "not connected")
        self._state_changed = asyncio.Condition()
        self._latest_jpeg: bytes | None = None
        self._latest_video_ts = 0.0
        self._video_frames = 0
        self._status = "starting"
        self._last_result = ""

    async def run(self) -> None:
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
        app.router.add_get("/video.mjpg", self._video_stream)
        app.router.add_get("/status.json", self._status_json)
        app.router.add_get("/api/maps", self._maps_list)
        app.router.add_post("/api/maps/save", self._map_save)
        app.router.add_post("/api/maps/load", self._map_load)
        app.router.add_post("/api/perception/check", self._perception_check)
        app.router.add_post("/api/autonomy/{action}", self._autonomy_action)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("AI autonomy GUI listening on http://%s:%s", self._host, self._port)

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
        self._perception = self._make_perception_provider()
        if self._map_path is not None:
            self._load_map(self._map_path)
        self._attach_video()
        self._status = "connected"

    async def _shutdown(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()
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

    async def _maps_list(self, _request: web.Request) -> web.Response:
        return web.json_response({"maps": list_patrol_maps(self._maps_dir)})

    async def _map_save(self, request: web.Request) -> web.Response:
        payload = await _json_or_empty(request)
        try:
            patrol_map = _patrol_map_from_payload(payload, require_route=False)
            path = save_patrol_map(patrol_map, self._maps_dir)
        except Exception as exc:  # noqa: BLE001
            log.exception("map save failed")
            return web.json_response({"ok": False, "result": f"map save failed: {exc}"}, status=400)

        result = f"saved draft {path.name}"
        try:
            patrol_map.validate_for_patrol()
        except ValueError as exc:
            if self._map_path is not None and self._map_path.resolve() == path.resolve():
                await self._unload_map()
            result = f"{result}; not patrol-ready: {exc}"
        else:
            self._load_map(path)
            result = f"saved and loaded {path.name}"
        return web.json_response({"ok": True, "result": result, "status": self._status_payload()})

    async def _map_load(self, request: web.Request) -> web.Response:
        payload = await _json_or_empty(request)
        name = str(payload.get("name", "")).strip()
        if not name:
            return web.json_response({"ok": False, "result": "name is required"}, status=400)
        path = self._maps_dir / f"{safe_map_filename(name)}.json"
        if not path.exists():
            candidate = self._maps_dir / name
            path = candidate if candidate.exists() else path
        try:
            self._load_map(path)
        except Exception as exc:  # noqa: BLE001
            log.exception("map load failed: %s", path)
            return web.json_response({"ok": False, "result": f"map load failed: {exc}"}, status=400)
        return web.json_response({"ok": True, "result": f"loaded {path.name}", "status": self._status_payload()})

    async def _perception_check(self, _request: web.Request) -> web.Response:
        await self._refresh_perception_health()
        return web.json_response({"ok": self._perception_ready(), "health": self._perception_health.__dict__})

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

    async def _autonomy_action(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        if action == "activate":
            ready_error = await self._activation_error()
            if ready_error:
                return web.json_response({"ok": False, "result": ready_error, "status": self._status_payload()}, status=400)
        if self._supervisor is None:
            return web.json_response({"ok": False, "result": "load and save a patrol-ready map before autonomy"}, status=503)
        try:
            if action == "activate":
                await self._supervisor.activate()
            elif action == "pause":
                await self._supervisor.pause()
            elif action == "resume":
                await self._supervisor.resume()
            elif action == "stop":
                await self._supervisor.stop()
            elif action == "step":
                await self._supervisor.step_once()
            else:
                return web.json_response({"ok": False, "result": f"unknown action {action!r}"}, status=400)
        except Exception as exc:  # noqa: BLE001
            log.exception("autonomy action failed: %s", action)
            return web.json_response({"ok": False, "result": f"{action} failed: {exc}"}, status=400)
        self._last_result = action
        return web.json_response({"ok": True, "result": action, "status": self._status_payload()})

    def _status_payload(self) -> dict[str, Any]:
        autonomy = self._supervisor.status().__dict__ if self._supervisor is not None else None
        map_payload = self._patrol_map.to_dict() if self._patrol_map is not None else empty_patrol_map().to_dict()
        return {
            "status": self._status,
            "video_frames": self._video_frames,
            "maps_dir": str(self._maps_dir),
            "map_path": str(self._map_path) if self._map_path is not None else None,
            "map_loaded": self._patrol_map is not None,
            "map": map_payload,
            "perception": self._perception_health.__dict__,
            "allow_no_detector": self._allow_no_detector,
            "detector": self._detector,
            "last_result": self._last_result,
            "autonomy": autonomy,
        }

    def _load_map(self, path: Path) -> None:
        patrol_map = load_patrol_map(path, require_route=True)
        assert self._client is not None
        assert self._perception is not None
        self._patrol_map = patrol_map
        self._map_path = path
        self._supervisor = AutonomySupervisor(patrol_map, AutonomyNavigator(self._client), self._perception)

    async def _unload_map(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()
        self._supervisor = None
        self._patrol_map = None
        self._map_path = None

    def _make_perception_provider(self) -> PerceptionProvider:
        if self._detector == "yolo":
            return YoloPerceptionProvider(
                lambda: self._latest_jpeg,
                model_name=self._yolo_model,
                threshold=self._yolo_threshold,
                device=self._yolo_device,
            )
        return CameraOnlyPerceptionProvider(lambda: self._latest_jpeg)

    async def _refresh_perception_health(self) -> None:
        if self._perception is None:
            self._perception_health = PerceptionHealth(False, "not-started", "perception provider is not initialized")
            return
        self._perception_health = await self._perception.health()

    def _perception_ready(self) -> bool:
        return self._perception_health.ready or self._allow_no_detector

    async def _activation_error(self) -> str:
        if self._patrol_map is None or self._supervisor is None:
            return "no patrol-ready map loaded; create/save/load a map first"
        try:
            self._patrol_map.validate_for_patrol()
        except ValueError as exc:
            return f"map is not patrol-ready: {exc}"
        await self._refresh_perception_health()
        if not self._perception_ready():
            return (
                "perception is not validated: "
                f"{self._perception_health.detail}; start with Step Once or rerun with --allow-no-detector"
            )
        return ""


async def _json_or_empty(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _patrol_map_from_payload(payload: dict[str, Any], *, require_route: bool = True) -> PatrolMap:
    if "map" in payload and isinstance(payload["map"], dict):
        patrol_map = patrol_map_from_dict(payload["map"], default_name="untitled")
    else:
        name = str(payload.get("name", "untitled"))
        waypoints_raw = payload.get("waypoints", [])
        if not isinstance(waypoints_raw, list):
            raise ValueError("waypoints must be a list")
        waypoints: dict[str, Waypoint] = {}
        for raw in waypoints_raw:
            if not isinstance(raw, dict):
                continue
            wp = Waypoint(
                name=str(raw.get("name", "")).strip(),
                x=float(raw.get("x", 0.0)),
                y=float(raw.get("y", 0.0)),
                yaw=float(raw.get("yaw", 0.0)),
                note=str(raw.get("note", "")),
            )
            if not wp.name:
                raise ValueError("waypoint name is required")
            waypoints[wp.name] = wp
        patrol_map = PatrolMap(
            name=name,
            waypoints=waypoints,
            patrol_route=_string_list(payload.get("patrol_route", [])),
            no_go_zones=_string_list(payload.get("no_go_zones", [])),
        )
    if require_route:
        patrol_map.validate_for_patrol()
    return patrol_map


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Go2 AI Autonomy</title>
  <style>
    :root { color-scheme: dark; --bg:#101113; --panel:#17191d; --line:#333841; --text:#e8e8e8; --muted:#aeb7c2; --danger:#8c1d2c; --ok:#2e6f4f; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:system-ui, Segoe UI, sans-serif; }
    header { height:46px; display:flex; align-items:center; justify-content:space-between; padding:0 14px; background:#1b1d21; border-bottom:1px solid var(--line); }
    main { height:calc(100vh - 46px); display:grid; grid-template-columns:minmax(340px, 430px) 1fr; }
    aside { padding:12px; overflow:auto; border-right:1px solid var(--line); background:var(--panel); }
    .video { background:#050505; min-width:0; min-height:0; display:flex; align-items:center; justify-content:center; }
    #video { width:100%; height:100%; object-fit:contain; display:block; }
    button, input, textarea, select { font:inherit; }
    button { width:100%; border:1px solid #3c4652; background:#242a31; color:#f1f1f1; border-radius:6px; padding:10px; cursor:pointer; margin-top:8px; }
    button:hover { background:#303843; }
    input, textarea, select { width:100%; border:1px solid #3c4652; background:#0e1012; color:#f1f1f1; border-radius:6px; padding:8px; }
    textarea { min-height:58px; resize:vertical; }
    .activate { background:var(--ok); border-color:#55a878; }
    .stop { background:var(--danger); border-color:#b72b3d; }
    h2 { font-size:14px; margin:16px 0 8px; color:var(--muted); }
    label { display:grid; gap:5px; margin-top:8px; color:var(--muted); font-size:12px; }
    pre { white-space:pre-wrap; word-break:break-word; background:#0e1012; border:1px solid var(--line); border-radius:6px; padding:8px; color:#cbd5df; min-height:44px; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .grid button { margin-top:0; }
    .row { display:grid; grid-template-columns:1.2fr .8fr .8fr .8fr; gap:6px; margin-top:6px; }
    @media (max-width:900px) { main { grid-template-columns:1fr; grid-template-rows:auto 60vh; } aside { border-right:0; border-bottom:1px solid var(--line); } }
  </style>
</head>
<body>
  <header><strong>Go2 AI Autonomy</strong><span id="top">starting</span></header>
  <main>
    <aside>
      <h2>Map Builder</h2>
      <label>Map name <input id="mapName" value="new-map"></label>
      <div id="waypoints"></div>
      <button onclick="addWaypoint()">Add Waypoint</button>
      <label>Patrol route <textarea id="route" placeholder="home, room_center, left_scan"></textarea></label>
      <label>No-go zones <textarea id="nogos" placeholder="stairs, loose_cables"></textarea></label>
      <button onclick="saveMap()">Save And Load Map</button>
      <div class="grid">
        <select id="savedMaps"></select>
        <button onclick="loadSelectedMap()">Load Saved Map</button>
      </div>
      <button onclick="checkPerception()">Check Image Detection</button>

      <h2>Autonomy</h2>
      <button class="activate" onclick="act('activate')">Activate AI Mode</button>
      <div class="grid">
        <button onclick="act('pause')">Pause</button>
        <button onclick="act('resume')">Resume</button>
        <button onclick="act('step')">Step Once</button>
        <button class="stop" onclick="act('stop')">STOP</button>
      </div>
      <h2>Status</h2>
      <pre id="status">waiting</pre>
      <h2>Map</h2>
      <pre id="mapStatus">waiting</pre>
      <h2>Observation</h2>
      <pre id="obs">waiting</pre>
      <h2>Event Log</h2>
      <pre id="events">waiting</pre>
    </aside>
    <section class="video"><img id="video" src="/video.mjpg" alt="Live robot video"></section>
  </main>
  <script>
    let loadedEditorPath = null;
    function splitList(value) {
      return value.split(",").map(v => v.trim()).filter(Boolean);
    }
    function addWaypoint(wp = {}) {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML = `
        <input placeholder="name" value="${wp.name || ""}">
        <input placeholder="x" type="number" step="0.1" value="${wp.x ?? 0}">
        <input placeholder="y" type="number" step="0.1" value="${wp.y ?? 0}">
        <input placeholder="yaw" type="number" step="1" value="${wp.yaw ?? 0}">
      `;
      document.getElementById("waypoints").appendChild(row);
    }
    function collectMap() {
      const rows = [...document.querySelectorAll("#waypoints .row")];
      const waypoints = rows.map(row => {
        const inputs = row.querySelectorAll("input");
        return {name: inputs[0].value.trim(), x: Number(inputs[1].value), y: Number(inputs[2].value), yaw: Number(inputs[3].value)};
      }).filter(wp => wp.name);
      return {
        name: document.getElementById("mapName").value.trim() || "untitled",
        waypoints,
        patrol_route: splitList(document.getElementById("route").value),
        no_go_zones: splitList(document.getElementById("nogos").value)
      };
    }
    function loadMapIntoEditor(map) {
      document.getElementById("mapName").value = map.name || "untitled";
      document.getElementById("waypoints").innerHTML = "";
      const entries = Object.entries(map.waypoints || {});
      if (!entries.length) addWaypoint({name:"home", x:0, y:0, yaw:0});
      for (const [name, wp] of entries) addWaypoint({name, ...wp});
      document.getElementById("route").value = (map.patrol_route || []).join(", ");
      document.getElementById("nogos").value = (map.no_go_zones || []).join(", ");
    }
    async function saveMap() {
      const res = await fetch("/api/maps/save", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(collectMap())});
      const data = await res.json().catch(() => ({result:"bad response"}));
      await refreshMaps();
      await refresh();
      if (!res.ok) alert(data.result || "map save failed");
    }
    async function refreshMaps() {
      const res = await fetch("/api/maps");
      const data = await res.json();
      const select = document.getElementById("savedMaps");
      select.innerHTML = "";
      for (const item of data.maps || []) {
        const option = document.createElement("option");
        option.value = item.filename;
        option.textContent = `${item.name} (${item.waypoint_count} wp, ${item.ready ? "ready" : "draft"})`;
        select.appendChild(option);
      }
    }
    async function loadSelectedMap() {
      const name = document.getElementById("savedMaps").value;
      const res = await fetch("/api/maps/load", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name})});
      const data = await res.json().catch(() => ({result:"bad response"}));
      await refresh();
      if (!res.ok) alert(data.result || "map load failed");
    }
    async function checkPerception() {
      const res = await fetch("/api/perception/check", {method:"POST"});
      const data = await res.json().catch(() => ({health:{detail:"bad response"}}));
      await refresh();
      alert(`${data.ok ? "READY" : "NOT READY"}: ${data.health?.detail || ""}`);
    }
    async function act(action) {
      const res = await fetch(`/api/autonomy/${action}`, {method:"POST"});
      const data = await res.json().catch(() => ({result:"bad response"}));
      await refresh();
      if (!res.ok) alert(data.result || `${action} failed`);
      return data;
    }
    async function refresh() {
      const res = await fetch("/status.json");
      const data = await res.json();
      const a = data.autonomy || {};
      document.getElementById("top").textContent = `${data.status} video=${data.video_frames} state=${a.state || "none"}`;
      document.getElementById("status").textContent = [
        `state: ${a.state}`,
        `active: ${a.active}`,
        `map: ${a.map_name}`,
        `waypoint: ${a.current_waypoint}`,
        `route_index: ${a.route_index}`,
        `last_action: ${a.last_action}`
      ].join("\\n");
      document.getElementById("mapStatus").textContent = [
        `loaded: ${data.map_loaded}`,
        `path: ${data.map_path || "none"}`,
        `name: ${data.map?.name}`,
        `waypoints: ${Object.keys(data.map?.waypoints || {}).join(", ") || "none"}`,
        `route: ${(data.map?.patrol_route || []).join(" -> ") || "none"}`,
        `perception: ${data.perception?.backend} ready=${data.perception?.ready} (${data.perception?.detail})`,
        `allow_no_detector: ${data.allow_no_detector}`
      ].join("\\n");
      if (data.map && data.map_loaded && data.map_path !== loadedEditorPath) {
        loadMapIntoEditor(data.map);
        loadedEditorPath = data.map_path;
      }
      document.getElementById("obs").textContent = a.last_observation || "none";
      document.getElementById("events").textContent = (a.events || []).slice(-20).join("\\n");
    }
    addWaypoint({name:"home", x:0, y:0, yaw:0});
    refreshMaps();
    setInterval(refresh, 1000);
    refresh();
  </script>
</body>
</html>
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Go2 AI-only autonomy browser GUI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8775)
    parser.add_argument("--maps-dir", default="maps", help="Directory for saved patrol maps")
    parser.add_argument("--map", default="", help="Optional patrol map JSON to load at startup")
    parser.add_argument("--detector", choices=["camera", "yolo"], default="camera")
    parser.add_argument("--yolo-model", default="yolov8n.pt")
    parser.add_argument("--yolo-threshold", type=float, default=0.55)
    parser.add_argument("--yolo-device", default="", help="Optional Ultralytics device, for example cuda:0 or cpu")
    parser.add_argument(
        "--allow-no-detector",
        action="store_true",
        help="Allow autonomy activation with camera-only perception while object detection is not configured",
    )
    return parser.parse_args()


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    map_path = Path(args.map) if args.map else None
    await AiAutonomyGui(
        args.host,
        args.port,
        Path(args.maps_dir),
        map_path,
        args.allow_no_detector,
        args.detector,
        args.yolo_model,
        args.yolo_threshold,
        args.yolo_device,
    ).run()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
