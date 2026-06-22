"""Full-screen video cockpit dedicated to an Xbox-style controller."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import math
import time
from typing import Any

from aiohttp import web
from PIL import Image, ImageDraw

from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient

log = logging.getLogger(__name__)

_ACTIONS = {
    "right_flip",
    "left_flip",
    "backstand",
    "jump",
    "pounce",
    "back_flip",
    "front_flip",
    "stand_up",
    "sit_down",
}
_ACTION_LOCK_S = {
    "right_flip": 2.2, "left_flip": 2.2, "front_flip": 2.2, "back_flip": 2.2,
    "backstand": 1.8, "jump": 1.2, "pounce": 1.4, "stand_up": 1.2, "sit_down": 1.2,
}


class ControllerCockpit:
    def __init__(self, host: str, port: int, *, simulation: bool = False) -> None:
        self._host = host
        self._port = port
        self._simulation = simulation
        self._client: Go2WebRTCClient | None = None
        self._changed = asyncio.Condition()
        self._latest_jpeg: bytes | None = None
        self._latest_video_ts = 0.0
        self._video_frames = 0
        self._status = "starting"
        self._last_action = "none"
        self._action_lock_until = 0.0
        self._sim_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_get("/", self._index)
        app.router.add_get("/video.mjpg", self._video_stream)
        app.router.add_get("/status.json", self._status_json)
        app.router.add_get("/ws/control", self._control_socket)
        app.router.add_post("/api/move", self._move)
        app.router.add_post("/api/stop", self._stop)
        app.router.add_post("/api/action", self._action)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("controller cockpit listening on http://%s:%s", self._host, self._port)
        try:
            if self._simulation:
                self._status = "simulated"
                self._sim_task = asyncio.create_task(self._simulation_frames(), name="go2-controller-sim")
            else:
                await self._connect()
            while True:
                await asyncio.sleep(3600)
        finally:
            if self._sim_task is not None:
                self._sim_task.cancel()
            if self._client is not None:
                await self._client.stop()
                await self._client.close()
            await runner.cleanup()

    async def _connect(self) -> None:
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
        self._status = "connecting"
        await self._client.connect()
        conn = getattr(self._client, "_conn", None)
        video = getattr(conn, "video", None)
        if video is None:
            raise RuntimeError("WebRTC video interface not found")
        video.switchVideoChannel(True)
        video.add_track_callback(self._recv_video_track)
        self._status = "connected"

    async def _recv_video_track(self, track: Any) -> None:
        while True:
            frame = await track.recv()
            await self._publish_image(frame.to_image())

    async def _publish_image(self, image: Image.Image) -> None:
        with io.BytesIO() as out:
            image.save(out, format="JPEG", quality=82)
            jpeg = out.getvalue()
        async with self._changed:
            self._latest_jpeg = jpeg
            self._latest_video_ts = time.time()
            self._video_frames += 1
            self._changed.notify_all()

    async def _simulation_frames(self) -> None:
        while True:
            width, height = 1280, 720
            t = time.monotonic()
            image = Image.new("RGB", (width, height), (4, 8, 12))
            draw = ImageDraw.Draw(image)
            for y in range(0, height, 32):
                shade = 14 + int(8 * math.sin(t + y / 45.0))
                draw.line((0, y, width, y), fill=(shade, shade + 5, shade + 10))
            draw.text((36, 36), "CONTROLLER COCKPIT SIMULATION", fill=(220, 230, 240))
            draw.text((36, 66), self._last_action, fill=(80, 170, 255))
            await self._publish_image(image)
            await asyncio.sleep(1 / 24)

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _status_json(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": self._status,
                "video_frames": self._video_frames,
                "last_action": self._last_action,
                "simulation": self._simulation,
            }
        )

    async def _video_stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"},
        )
        await response.prepare(request)
        last_sent = 0.0
        while True:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: self._latest_jpeg is not None and self._latest_video_ts != last_sent
                )
                jpeg = self._latest_jpeg
                last_sent = self._latest_video_ts
            if jpeg is not None:
                await response.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")

    async def _move(self, request: web.Request) -> web.Response:
        payload = await _json(request)
        vx = float(payload.get("vx", 0.0))
        vy = float(payload.get("vy", 0.0))
        vyaw = float(payload.get("vyaw", 0.0))
        if time.monotonic() < self._action_lock_until:
            return web.json_response({"ok": True, "result": f"action in progress: {self._last_action}"})
        if self._client is not None:
            vx, vy, vyaw = self._client.publish_velocity(vx, vy, vyaw)
        self._last_action = f"drive vx={vx:.2f} vy={vy:.2f} yaw={vyaw:.2f}"
        return web.json_response({"ok": True, "result": self._last_action})

    async def _control_socket(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(heartbeat=10.0)
        await socket.prepare(request)
        try:
            async for message in socket:
                if message.type != web.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                except (TypeError, json.JSONDecodeError):
                    continue
                kind = payload.get("type")
                if kind == "move" and time.monotonic() >= self._action_lock_until:
                    vx = float(payload.get("vx", 0.0))
                    vy = float(payload.get("vy", 0.0))
                    vyaw = float(payload.get("vyaw", 0.0))
                    if self._client is not None:
                        vx, vy, vyaw = self._client.publish_velocity(vx, vy, vyaw)
                    self._last_action = f"drive vx={vx:.2f} vy={vy:.2f} yaw={vyaw:.2f}"
                elif kind == "stop":
                    if self._client is not None:
                        await self._client.stop()
                    self._last_action = "stop"
        finally:
            if self._client is not None:
                await self._client.stop()
        return socket

    async def _stop(self, _request: web.Request) -> web.Response:
        if self._client is not None:
            await self._client.stop()
        self._last_action = "stop"
        return web.json_response({"ok": True, "result": "stop"})

    async def _action(self, request: web.Request) -> web.Response:
        payload = await _json(request)
        action = str(payload.get("action", "")).strip().lower()
        if action not in _ACTIONS:
            return web.json_response({"ok": False, "result": f"unknown action {action!r}"}, status=400)
        await self._run_action(action)
        return web.json_response({"ok": True, "result": action})

    async def _run_action(self, action: str) -> None:
        self._action_lock_until = time.monotonic() + _ACTION_LOCK_S[action]
        if self._client is not None:
            await self._client.stop()
            if action == "stand_up":
                await self._client.stand_up()
            elif action == "sit_down":
                await self._client.sit_down()
            else:
                await self._client.advanced_action(action)
        self._last_action = action


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return value if isinstance(value, dict) else {}


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Go2 Controller</title>
  <style>
    :root { color-scheme:dark; font-family:Inter,ui-sans-serif,system-ui,Segoe UI,sans-serif; }
    * { box-sizing:border-box; }
    html,body { margin:0; width:100%; height:100%; overflow:hidden; background:#000; color:#fff; }
    main { position:relative; width:100%; height:100%; display:grid; place-items:center; background:#000; }
    #video { width:100%; height:100%; object-fit:contain; display:block; }
    .hud { position:absolute; left:18px; right:18px; bottom:18px; display:flex; gap:12px; align-items:flex-end; justify-content:space-between; pointer-events:none; }
    .panel { max-width:min(760px,72vw); padding:12px 14px; border:1px solid rgba(255,255,255,.18); border-radius:12px; background:rgba(4,8,12,.72); backdrop-filter:blur(12px); box-shadow:0 10px 35px rgba(0,0,0,.4); }
    .state { display:flex; align-items:center; gap:9px; font-size:13px; }
    .dot { width:9px; height:9px; border-radius:50%; background:#e4b84d; box-shadow:0 0 12px currentColor; }
    .dot.ready { background:#46d381; }
    #padName { color:#aab6c0; max-width:440px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .meters { display:grid; grid-template-columns:repeat(3,minmax(90px,1fr)); gap:8px; margin-top:10px; }
    .meter { display:grid; grid-template-columns:auto 1fr; gap:8px; align-items:center; color:#afbbc5; font-size:11px; }
    .track { height:6px; border-radius:99px; background:#26313a; overflow:hidden; }
    .fill { height:100%; width:0; border-radius:inherit; background:#50a7ff; transition:width .04s linear; }
    #ltFill { background:#cb83ff; }
    .mapping { margin-top:10px; display:flex; flex-wrap:wrap; gap:6px; }
    .mapping span { padding:3px 7px; border-radius:6px; background:rgba(255,255,255,.08); color:#cbd4db; font-size:11px; white-space:nowrap; }
    .speed { min-width:145px; text-align:right; }
    .speed strong { display:block; font-size:28px; line-height:1; }
    .speed small,#lastAction { color:#aab6c0; font-size:12px; }
    @media(max-width:720px){.mapping{display:none}.panel{max-width:65vw}.meters{grid-template-columns:1fr}.meter:nth-child(3){display:none}.speed strong{font-size:22px}}
  </style>
</head>
<body>
  <main>
    <img id="video" src="/video.mjpg" alt="Live Go2 video">
    <div class="hud">
      <div class="panel">
        <div class="state"><span class="dot" id="dot"></span><strong id="padState">Connect controller</strong><span id="padName"></span></div>
        <div class="meters">
          <div class="meter"><span>RT RUN</span><span class="track"><span class="fill" id="rtFill"></span></span></div>
          <div class="meter"><span>LT BACKSTAND</span><span class="track"><span class="fill" id="ltFill"></span></span></div>
          <div class="meter"><span>OUTPUT</span><span id="driveOutput">0.00</span></div>
        </div>
        <div class="mapping">
          <span>RB Right Flip</span><span>LB Left Flip</span><span>Y Stand</span><span>B Sit</span>
          <span>A Jump</span><span>X Pounce</span><span>D↑ Front Flip</span><span>D↓ Back Flip</span>
          <span>D←/→ Speed</span>
        </div>
      </div>
      <div class="panel speed"><small>SPEED</small><strong id="speedValue">1.40</strong><span id="lastAction">waiting</span></div>
    </div>
  </main>
  <script>
    const speedSteps = [0.60, 1.00, 1.40, 1.80, 2.00];
    let speedIndex = Math.max(0, Math.min(speedSteps.length - 1, Number(localStorage.getItem("go2ControllerSpeed") ?? 4)));
    let filteredRT = 0, filteredLT = 0;
    let current = {vx:0, vy:0, vyaw:0};
    let lastFrame = performance.now(), lastMoveSent = 0, moving = false;
    let ltHoldMs = 0, ltLatched = false, actionLockUntil = 0;
    const edgeState = new Map();
    const dot = document.getElementById("dot");
    const padState = document.getElementById("padState");
    const padName = document.getElementById("padName");
    const rtFill = document.getElementById("rtFill");
    const ltFill = document.getElementById("ltFill");
    const driveOutput = document.getElementById("driveOutput");
    const speedValue = document.getElementById("speedValue");
    const lastAction = document.getElementById("lastAction");
    let controlSocket = null;

    function connectControl() {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      controlSocket = new WebSocket(`${scheme}://${location.host}/ws/control`);
      controlSocket.addEventListener("close", () => setTimeout(connectControl, 250));
    }
    connectControl();

    function updateSpeed(delta=0) {
      speedIndex = Math.max(0, Math.min(speedSteps.length - 1, speedIndex + delta));
      localStorage.setItem("go2ControllerSpeed", String(speedIndex));
      speedValue.textContent = speedSteps[speedIndex].toFixed(2);
    }
    updateSpeed();
    function buttonValue(pad,index){const b=pad.buttons[index];return Number(b?.value ?? (b?.pressed?1:0));}
    function down(pad,index){const b=pad.buttons[index];return Boolean(b?.pressed)||Number(b?.value||0)>.5;}
    function deadzone(value,zone=.08){if(Math.abs(value)<=zone)return 0;return Math.sign(value)*(Math.abs(value)-zone)/(1-zone);}
    function smoothstep(value){value=Math.max(0,Math.min(1,value));return value*value*(3-2*value);}
    function smooth(currentValue,target,dt,tau){return currentValue+(target-currentValue)*(1-Math.exp(-dt/tau));}
    async function post(path,body={}) {
      const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const data=await response.json().catch(()=>({result:"bad response"}));
      if(data.result)lastAction.textContent=data.result;
      return data;
    }
    function edge(pad,index,key,callback){const value=down(pad,index);const prior=edgeState.get(key)||false;if(value&&!prior)callback();edgeState.set(key,value);}
    function action(name){
      const lockMs={right_flip:2200,left_flip:2200,front_flip:2200,back_flip:2200,backstand:1800,jump:1200,pounce:1400,stand_up:1200,sit_down:1200}[name]||1000;
      actionLockUntil=performance.now()+lockMs;moving=false;current={vx:0,vy:0,vyaw:0};post("/api/action",{action:name}).catch(()=>{});
    }
    function sendMove(body){
      const now=performance.now();
      if(now-lastMoveSent<20)return;
      lastMoveSent=now;
      const payload=JSON.stringify({type:"move",...body});
      if(controlSocket?.readyState===WebSocket.OPEN)controlSocket.send(payload);
      else fetch("/api/move",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}).catch(()=>{});
    }
    function stop(){if(moving){moving=false;if(controlSocket?.readyState===WebSocket.OPEN)controlSocket.send('{"type":"stop"}');else post("/api/stop").catch(()=>{});}current={vx:0,vy:0,vyaw:0};}
    function poll(now){
      const dt=Math.min(.08,Math.max(.001,(now-lastFrame)/1000));lastFrame=now;
      const pad=Array.from(navigator.getGamepads?.()||[]).find(Boolean);
      if(!pad){dot.className="dot";padState.textContent="Connect controller";padName.textContent="";filteredRT=smooth(filteredRT,0,dt,.04);filteredLT=smooth(filteredLT,0,dt,.05);stop();requestAnimationFrame(poll);return;}
      dot.className="dot ready";padState.textContent="Controller active";padName.textContent=pad.id;

      filteredRT=smooth(filteredRT,buttonValue(pad,7),dt,.04);
      filteredLT=smooth(filteredLT,buttonValue(pad,6),dt,.05);
      const run=smoothstep(Math.max(0,(filteredRT-.025)/.975));
      rtFill.style.width=`${run*100}%`;ltFill.style.width=`${Math.min(1,ltHoldMs/160)*100}%`;

      if(filteredLT>.62&&!ltLatched){ltHoldMs+=dt*1000;if(ltHoldMs>=160){ltLatched=true;action("backstand");}}
      else if(filteredLT<.32&&!ltLatched){ltHoldMs=0;}
      if(filteredLT<.12){ltLatched=false;ltHoldMs=0;}

      edge(pad,5,"rb",()=>action("right_flip"));edge(pad,4,"lb",()=>action("left_flip"));
      edge(pad,3,"y",()=>action("stand_up"));edge(pad,1,"b",()=>action("sit_down"));
      edge(pad,0,"a",()=>action("jump"));edge(pad,2,"x",()=>action("pounce"));
      edge(pad,12,"dup",()=>action("front_flip"));edge(pad,13,"ddown",()=>action("back_flip"));
      edge(pad,14,"dleft",()=>updateSpeed(-1));edge(pad,15,"dright",()=>updateSpeed(1));

      if(now<actionLockUntil){current={vx:0,vy:0,vyaw:0};driveOutput.textContent="ACTION";requestAnimationFrame(poll);return;}
      const speed=speedSteps[speedIndex];
      const target={vx:-deadzone(pad.axes[1]||0)*speed+run*speed,vy:-deadzone(pad.axes[0]||0)*Math.min(speed,.95),vyaw:-deadzone(pad.axes[2]||0)*2.35};
      current.vx=smooth(current.vx,target.vx,dt,.025);current.vy=smooth(current.vy,target.vy,dt,.025);current.vyaw=smooth(current.vyaw,target.vyaw,dt,.022);
      driveOutput.textContent=`${current.vx.toFixed(2)} / ${current.vyaw.toFixed(2)}`;
      if(Math.abs(current.vx)>.01||Math.abs(current.vy)>.01||Math.abs(current.vyaw)>.01){moving=true;sendMove(current);}else stop();
      requestAnimationFrame(poll);
    }
    window.addEventListener("gamepaddisconnected",stop);
    window.addEventListener("blur",stop);
    window.addEventListener("pagehide",()=>navigator.sendBeacon?.("/api/stop",new Blob(["{}"],{type:"application/json"})));
    requestAnimationFrame(poll);
  </script>
</body>
</html>"""


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Video-only Go2 controller cockpit")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8773)
    parser.add_argument("--sim", action="store_true", help="Use generated video and no robot")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await ControllerCockpit(args.host, args.port, simulation=args.sim).run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
