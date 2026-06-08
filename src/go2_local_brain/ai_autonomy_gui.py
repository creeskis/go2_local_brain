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

from .autonomy.map import load_patrol_map
from .autonomy.navigator import AutonomyNavigator
from .autonomy.perception import NullPerceptionProvider
from .autonomy.supervisor import AutonomySupervisor
from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient
from .viewer import _jpeg_from_frame

log = logging.getLogger(__name__)


class AiAutonomyGui:
    """Browser shell for activating and watching autonomous patrol."""

    def __init__(self, host: str, port: int, map_path: Path) -> None:
        self._host = host
        self._port = port
        self._map_path = map_path
        self._client: Go2WebRTCClient | None = None
        self._supervisor: AutonomySupervisor | None = None
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
        patrol_map = load_patrol_map(self._map_path)
        perception = NullPerceptionProvider(lambda: self._latest_jpeg)
        self._supervisor = AutonomySupervisor(patrol_map, AutonomyNavigator(self._client), perception)
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
        if self._supervisor is None:
            return web.json_response({"ok": False, "result": "autonomy supervisor is not ready"}, status=503)
        action = request.match_info["action"]
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
        return {
            "status": self._status,
            "video_frames": self._video_frames,
            "map_path": str(self._map_path),
            "last_result": self._last_result,
            "autonomy": autonomy,
        }


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
    button { width:100%; border:1px solid #3c4652; background:#242a31; color:#f1f1f1; border-radius:6px; padding:10px; cursor:pointer; margin-top:8px; font:inherit; }
    button:hover { background:#303843; }
    .activate { background:var(--ok); border-color:#55a878; }
    .stop { background:var(--danger); border-color:#b72b3d; }
    h2 { font-size:14px; margin:16px 0 8px; color:var(--muted); }
    pre { white-space:pre-wrap; word-break:break-word; background:#0e1012; border:1px solid var(--line); border-radius:6px; padding:8px; color:#cbd5df; min-height:44px; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .grid button { margin-top:0; }
    @media (max-width:900px) { main { grid-template-columns:1fr; grid-template-rows:auto 60vh; } aside { border-right:0; border-bottom:1px solid var(--line); } }
  </style>
</head>
<body>
  <header><strong>Go2 AI Autonomy</strong><span id="top">starting</span></header>
  <main>
    <aside>
      <button class="activate" onclick="act('activate')">Activate AI Mode</button>
      <div class="grid">
        <button onclick="act('pause')">Pause</button>
        <button onclick="act('resume')">Resume</button>
        <button onclick="act('step')">Step Once</button>
        <button class="stop" onclick="act('stop')">STOP</button>
      </div>
      <h2>Status</h2>
      <pre id="status">waiting</pre>
      <h2>Observation</h2>
      <pre id="obs">waiting</pre>
      <h2>Event Log</h2>
      <pre id="events">waiting</pre>
    </aside>
    <section class="video"><img id="video" src="/video.mjpg" alt="Live robot video"></section>
  </main>
  <script>
    async function act(action) {
      const res = await fetch(`/api/autonomy/${action}`, {method:"POST"});
      const data = await res.json().catch(() => ({result:"bad response"}));
      await refresh();
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
      document.getElementById("obs").textContent = a.last_observation || "none";
      document.getElementById("events").textContent = (a.events || []).slice(-20).join("\\n");
    }
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
    parser.add_argument("--map", default="maps/home.json", help="Path to patrol map JSON")
    return parser.parse_args()


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    await AiAutonomyGui(args.host, args.port, Path(args.map)).run()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
