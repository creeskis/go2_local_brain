# go2_local_brain

A local control stack for the **Unitree Go2 Air**. You run it on your own
machine (WSL/Linux), it connects to the dog over the dog's own Wi‑Fi using
WebRTC, and it gives you a **browser cockpit**: live video, keyboard driving
with multiple movement modes, facial recognition, LiDAR-based mapping and
autonomous patrol, and an optional (off-by-default) Nerf trigger.

Nothing runs on the dog except the firmware it already has. No cloud, no
account, no LLM required for driving.

---

## How it works (the mental model)

```
   Your computer (WSL)                         Unitree Go2 Air
   ┌───────────────────────────┐               ┌──────────────────────┐
   │  python -m go2_local_brain │   WebRTC      │  WebRTC service      │
   │       .ai_autonomy_gui     │◀────────────▶│  (video, LiDAR,      │
   │                            │  Wi‑Fi LAN    │   odometry, motion)  │
   │  • aiohttp web server      │  :9991        │                      │
   │  • driver + perception     │               └──────────────────────┘
   └───────────┬────────────────┘
               │ http://127.0.0.1:8775/?token=…
               ▼
   ┌───────────────────────────┐
   │  Your browser (cockpit)    │
   │  video · WASD · faces ·    │
   │  patrol · LiDAR · nerf     │
   └───────────────────────────┘
```

Three layers, top to bottom:

1. **The dog** speaks WebRTC over its Wi‑Fi (default IP `192.168.123.121`,
   signaling on port `9991`). It streams camera + LiDAR + pose and accepts
   motion/sport commands. You don't install anything on it.
2. **The Python app** (this repo) runs on your computer. It opens the WebRTC
   connection, wraps it in a safe driver (velocity clamps + a dead‑man stop),
   runs perception (face recognition, LiDAR mapping) locally, and serves a
   web cockpit.
3. **Your browser** is the cockpit. The Python app and the browser are on the
   same machine, so the heavy work (decoding video/LiDAR, recognizing faces)
   happens in Python, and the browser just shows the result and sends button
   presses back.

> A Jetson is **optional** and only used as a USB relay for the Nerf blaster
> (see [docs/faces_workflows_targeting.md](docs/faces_workflows_targeting.md)).
> You do not need it to drive, see video, recognize faces, or map.

---

## Quickstart on WSL

You need the dog powered on and joined to the same Wi‑Fi as your computer.

For the current lean localhost cockpit with WASD, video, FaceID boxes, and the
Jetson USB trigger relay:

```bash
cd ~/robotics/go2_local_brain
git pull
source .venv/bin/activate
./scripts/run_local_cockpit.sh
```

Open:

```text
http://127.0.0.1:8775
```

Defaults:

```env
GO2_IP=192.168.123.161
GO2_WEBRTC_METHOD=LocalSTA
GO2_AES_128_KEY=
FORCE_MOTION_MODE=normal
GUN_DOG_HOST=192.168.123.121
GUN_DOG_USER=root
GUN_DOG_PASSWORD=
GUN_JETSON_HOST=10.42.0.2
GUN_JETSON_USER=unitree
GUN_JETSON_PASSWORD=
GUN_JETSON_SUDO_PASSWORD=
GUN_LOCAL_SSH_PORT=10022
GUN_TUNNEL_SCRIPT=scripts/gun_tunnel_manual.sh
GUN_COMMAND_SCRIPT=scripts/gun_command_manual.sh
GUN_FIRE_SCRIPT=scripts/gun_fire_manual.sh
GUN_STOP_SCRIPT=scripts/gun_stop_manual.sh
GUN_TEST_SCRIPT=scripts/gun_test_manual.sh
GUN_FIRE_COMMAND="cat /dev/ttyUSB0 | xxd"
GUN_STOP_COMMAND="printf '\\x30' > /dev/ttyUSB0"
GO2_FACE_BACKEND=face_recognition
```

The cockpit keeps an SSH tunnel to the dog open with `scripts/gun_tunnel_manual.sh`.
`Start Fire` and `Stop Fire` run short Jetson commands through that tunnel with
`scripts/gun_command_manual.sh`. Releasing the mouse button or Xbox trigger
does not stop firing.

The local cockpit uses operator-speed movement caps: up to `2.0 m/s` forward,
`1.0 m/s` strafe, and `2.5 rad/s` yaw, with browser-side smoothing so blended
WASD/QE turns ramp instead of snapping. Press `Space`, Xbox `A`, or the `Jump`
button for the firmware jump action. Xbox sticks also drive the dog: left stick
moves, right stick turns, right trigger starts fire, and `B` stops fire.

Install `expect` in the WSL instance, then set `GUN_DOG_PASSWORD` and
`GUN_JETSON_PASSWORD` in your local `.env`. If sudo prompts separately on the
Jetson, set `GUN_JETSON_SUDO_PASSWORD` in your private `.env`. Do not commit
those password values.

```bash
sudo apt install -y expect
```

The gun buttons use this path:

```text
computer -> ssh root@192.168.123.121 -> ssh unitree@10.42.0.2
```

`Test Script` opens the tunnel, pipes `GUN_JETSON_SUDO_PASSWORD` into
`sudo -S chmod 666 /dev/ttyUSB0` on the Jetson, and expects `OK TEST`.
`Start Fire` runs the same permission command before starting
`cat /dev/ttyUSB0 | xxd` in its own process group on the Jetson. `Stop Fire`
kills only that saved process group, runs the same chmod, then runs
`printf '\x30' > /dev/ttyUSB0`.

FaceID enrollment requires a face embedding backend. For CPU use:

```bash
pip install face_recognition
```

Then click `Enroll Face`, enter a name, and the current visible face is stored
in `~/.config/go2_local_brain/faces.json` by default.

Full install from scratch:

```bash
# 1. System packages. portaudio19-dev is required because a dependency
#    (pyaudio) compiles against it on Linux.
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl portaudio19-dev

# 2. Get the code + a virtual environment
cd ~ && mkdir -p robotics && cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

# 3. Point it at the dog
cp .env.example .env
#   edit .env and make sure: GO2_IP=192.168.123.121

# 4. Confirm your machine can reach the dog
ping -c 3 192.168.123.121          # must reply
python scripts/smoke_test_imports.py   # prints "imports ok"

# 5. Launch the cockpit
python -m go2_local_brain.ai_autonomy_gui
```

On startup it prints a line like:

```
=== Open: http://127.0.0.1:8775/?token=ab12…cd
```

Open that exact URL (token included) in your browser. The token gates every
control action, so bookmark the URL or copy it from the terminal each run.

### Reaching the cockpit from a Windows browser

- **WSL2 mirrored networking** (`networkingMode=mirrored` in
  `%USERPROFILE%\.wslconfig`): Windows and WSL share `localhost`, so the
  printed `http://127.0.0.1:8775/?token=…` URL works directly in Edge/Chrome.
- **Otherwise:** start it on all interfaces and reach it by the WSL IP:
  ```bash
  python -m go2_local_brain.ai_autonomy_gui --bind-public
  ip addr show eth0 | grep 'inet '      # find the WSL IP, then browse to that:8775
  ```
  The auth token still applies, so only someone with the URL can drive.

---

## The cockpit, panel by panel

The cockpit is `ai_autonomy_gui` on port **8775**. Right-rail panels:

| Panel | What it does |
| --- | --- |
| **Live video** | MJPEG stream from the dog's camera, with face/detection overlays. |
| **Drive modes** | Switch between `normal` / `flip` / `jump` / `backstand`. See below. |
| **Speed** | Cycle `slow` → `normal` → `fast` (always inside the driver's hard limits). |
| **Keyboard** | "Enable keyboard", then WASD/QE drive (details below). |
| **Quick override** | Stand / Sit / **STOP** buttons. |
| **Faces** | See who the dog recognizes; save a new face with a name. |
| **Autonomy / patrol** | Start/stop autonomous patrol of a saved map. |
| **Nerf** | Off by default; arm/fire only appears when launched with the nerf relay. |
| **Status / events** | Live state, last action, event log. |

### Driving (WASD + modes)

Click **Enable keyboard** first (so keystrokes are captured, not typed into a
text box). Then:

- **`W` / `S`** — forward / back
- **`A` / `D`** — strafe left / right
- **`Q` / `E`** — turn left / right
- **`Space`** — stop
- **`Esc`** — disable keyboard capture

**Modes change what the keys mean**, and they're honest about what the Go2
firmware actually supports:

| Mode | Behavior |
| --- | --- |
| **normal** | Hold keys to drive continuously (real velocity control). This is the everyday mode. |
| **flip** | One press = one flip: `W`/`S`/`A`/`D` → front/back/left/right flip. |
| **jump** | `W` = forward jump. The firmware has no side/back jump, so other keys do nothing. |
| **backstand** | Enters the hind-leg posture (a static one-shot). The firmware can't *drive* while in it, so WASD is disabled in this mode — it's a pose, not a gait. |

The speed toggle scales velocity in **normal** mode. Flip/jump/backstand are
discrete firmware actions, so speed doesn't apply to them.

### Faces

Face recognition runs in Python over the video stream. When the cockpit sees
an **unknown** face, a **Save this face** button appears — click it, type a
name, and it's stored. Recognized faces are labeled on the video and persist
across runs.

The database is a JSON file at `~/.config/go2_local_brain/faces.json`
(override with `GO2_FACE_DB`). You can also enroll from the command line:

```bash
# From a photo:
python scripts/enroll_face.py --label alex --image alex.jpg

# From the dog's live camera (grabs a few frames):
python scripts/enroll_face.py --label alex --camera --shots 5
```

Face recognition needs one extra backend installed (it's optional so the core
app stays light):

```bash
pip install -e ".[faces]"        # InsightFace (ONNX) — recommended
# or
pip install face_recognition     # dlib — needs cmake; simpler on CPU
```

---

## Configuration (`.env`)

Copy `.env.example` to `.env` and edit. The only required value is `GO2_IP`.

| Variable | Meaning |
| --- | --- |
| `GO2_IP` | The dog's Wi‑Fi IP. **`192.168.123.121`** — this is the dog, not anything else. |
| `GO2_WEBRTC_METHOD` | `LocalSTA` for a dog on your LAN (the normal case). |
| `GO2_AES_128_KEY` | Leave blank unless the firmware's handshake demands a per‑device key. |
| `OLLAMA_MODEL` | Only used by the optional LLM "brain" REPL; irrelevant for the cockpit. |
| `FORCE_MOTION_MODE` | Set to `normal` only if sport commands seem to be silently ignored. |
| `VERBOSE_WEBRTC_LOGS` | `1` to see every incoming WebRTC packet (debugging only). |

---

## Other entry points

| Command | What it is |
| --- | --- |
| `python -m go2_local_brain.ai_autonomy_gui` | **The main cockpit** (everything above), port 8775. |
| `python -m go2_local_brain.viewer` | Read‑only live video + LiDAR view, port 8765. |
| `python -m go2_local_brain.face_viewer` | Focused face‑recognition video page, port 8776. |
| `python -m go2_local_brain.diagnose_webrtc` | Test the WebRTC handshake to the dog and print why it failed. |
| `python -m go2_local_brain.diagnose_video` | Confirm the camera track decodes. |
| `python -m go2_local_brain.main` | Optional text‑prompt LLM "brain" REPL (needs Ollama; not needed for driving). |

`viewer` and `face_viewer` default to `--host 0.0.0.0`; the cockpit defaults
to loopback (`127.0.0.1`) because it can move the robot.

---

## Testing

The logic (control modes, face matching, mapping math, autonomy, safety
gates) is unit‑tested and runs without a robot or any ML/GPU installed:

```bash
source .venv/bin/activate
python -m unittest discover -s tests
```

All tests should pass before you rely on a change.

---

## Troubleshooting

**`ping 192.168.123.121` fails.** Your computer isn't on the same network as
the dog. Join the dog's Wi‑Fi (or put both on the same LAN). On WSL2 without
mirrored networking, the WSL VM may not see the LAN — enable mirrored
networking or run from a machine that can reach the dog.

**Cockpit starts but logs "signaling port … not exposing".** The dog's WebRTC
service isn't answering on `:9991`. Reboot the dog, or restart its WebRTC
service. Confirm with: `curl -m 3 http://192.168.123.121:9991/con_notify`
(should return a base64 blob, not time out).

**`pip install -e .` fails building `pyaudio`.** Install the header package:
`sudo apt install -y portaudio19-dev`, then retry.

**Video panel is black.** The handshake connected but the H.264 track isn't
decoding. Check the terminal for decoder errors; `pip install --upgrade av`
often fixes a stale decoder.

**Browser can't open the cockpit URL.** Either the token is missing/old (copy
the fresh `?token=…` line from the terminal) or you're not loopback‑reachable
— relaunch with `--bind-public` and use the WSL IP.

**Robot ignores motion commands.** Set `FORCE_MOTION_MODE=normal` in `.env`
and relaunch; some firmware needs an explicit switch into normal sport mode.

---

## Safety

- The driver clamps every velocity to hard limits and runs a dead‑man stop:
  if commands stop arriving, the dog stops.
- The cockpit's control endpoints require the bearer token; loopback is the
  default bind. Use `--bind-public` only on a network you trust.
- The Nerf trigger is **disarmed by default** and is only wired up when you
  explicitly launch the relay; it requires an arm step, a centered/locked
  target, a cooldown, and a per‑session shot cap. It fires foam darts — still,
  treat the arm switch like an interlock.
- Flips and jumps are real, forceful firmware actions. Give the dog clear
  space before using `flip`/`jump` modes.

---

## Repository layout

```
src/go2_local_brain/
  ai_autonomy_gui.py     the main browser cockpit (video, WASD/modes, faces,
                         patrol, LiDAR, nerf)
  viewer.py              read-only video + LiDAR viewer
  face_viewer.py         face-recognition video page
  main.py                optional LLM "brain" REPL
  driver/webrtc_client.py   WebRTC connection + safe motion driver
  autonomy/
    control_modes.py     WASD + speed + mode resolver (pure, tested)
    face_id.py           face embeddings + database + matching
    face_tracker.py      stable face tracks across frames
    local_map.py         origin-locked pose for a stable map frame
    lidar_map.py         LiDAR -> obstacle field + occupancy
    map.py               saved maps + waypoints
    navigator.py         waypoint navigation primitives
    supervisor.py        autonomous patrol state machine
    targeting.py         phone-user targeting + Nerf control
  safety/limits.py       velocity caps + dead-man timeout
  config.py              reads .env
scripts/                 enroll_face.py, diagnostics, deploy helpers
tests/                   unit tests (no hardware needed)
docs/                    deeper guides (see below)
```

### Deeper docs

- [docs/module_reference.md](docs/module_reference.md) — **how every module
  works and gets called**, with end-to-end call chains.
- [docs/how_it_works.md](docs/how_it_works.md) — the WebRTC + driver internals.
- [docs/faces_workflows_targeting.md](docs/faces_workflows_targeting.md) —
  face recognition, workflows, and the Nerf relay design.
- [docs/ai_autonomy.md](docs/ai_autonomy.md) — autonomy / patrol details.
- [docs/code_walkthrough.md](docs/code_walkthrough.md) — file-by-file tour.
- [docs/jetson_orin_deploy.md](docs/jetson_orin_deploy.md) /
  [docs/jetson_networking.md](docs/jetson_networking.md) — optional Jetson use.
