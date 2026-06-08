# Browser GUI Modes

This repo has several browser entry points so experiments can stay isolated. Each process opens one WebRTC connection to the Go2, serves a local web page, and exposes only the features listed for that mode.

Run all commands from the repo root:

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
pip install -e .
```

## Quick Pick

| Need | Module | Default port |
| --- | --- | --- |
| AI prompt box + video, with hidden WASD/QE keyboard control | `go2_local_brain.ai_cli_video_gui` | `8771` |
| AI prompt box + video + LiDAR, no keyboard driving | `go2_local_brain.ai_lidar_gui` | `8772` |
| WASD/QE keyboard driving + video, no AI or LiDAR | `go2_local_brain.wasd_video_gui` | `8773` |
| AI prompt box + WASD/QE + video + LiDAR | `go2_local_brain.ai_wasd_lidar_gui` | `8774` |
| Manual cockpit with exact sport-command buttons | `go2_local_brain.control_gui` | `8770` |
| Original combined GUI | `go2_local_brain.gui` | `8765` |

## Run Commands

```bash
python -m go2_local_brain.ai_cli_video_gui --host 0.0.0.0 --port 8771
python -m go2_local_brain.ai_lidar_gui --host 0.0.0.0 --port 8772
python -m go2_local_brain.wasd_video_gui --host 0.0.0.0 --port 8773
python -m go2_local_brain.ai_wasd_lidar_gui --host 0.0.0.0 --port 8774
python -m go2_local_brain.control_gui --host 0.0.0.0 --port 8770
python -m go2_local_brain.gui --host 0.0.0.0 --port 8765
```

Open the matching URL from the host browser, for example:

```text
http://localhost:8774
```

If `localhost` does not route into the WSL instance:

```bash
hostname -I
```

Then open:

```text
http://<wsl-ip>:8774
```

## What Each Mode Starts

### 1. AI CLI + Video

```bash
python -m go2_local_brain.ai_cli_video_gui --host 0.0.0.0 --port 8771
```

Starts:

- WebRTC robot connection.
- Live MJPEG video.
- Ollama-backed AI command box.
- WASD/QE keyboard movement endpoint.

The page intentionally does not show a drive panel. Keyboard movement is still enabled:

- `W`: forward
- `S`: back
- `A`: strafe left
- `D`: strafe right
- `Q`: turn left
- `E`: turn right

### 2. AI + Video + LiDAR

```bash
python -m go2_local_brain.ai_lidar_gui --host 0.0.0.0 --port 8772
```

Starts:

- WebRTC robot connection.
- Live MJPEG video.
- Ollama-backed AI command box.
- LiDAR switch-on, LiDAR subscription, and Three.js point-cloud rendering.

Does not start keyboard movement controls. Use this mode when testing AI and sensing without accidental keyboard drive commands.

### 3. WASD + Video

```bash
python -m go2_local_brain.wasd_video_gui --host 0.0.0.0 --port 8773
```

Starts:

- WebRTC robot connection.
- Live MJPEG video.
- Visible drive panel.
- WASD/QE keyboard movement.
- Locomotion mode buttons.

Does not start Ollama or LiDAR. This is the cleanest movement test mode.

### 4. AI + WASD + Video + LiDAR

```bash
python -m go2_local_brain.ai_wasd_lidar_gui --host 0.0.0.0 --port 8774
```

Starts everything in one process:

- WebRTC robot connection.
- Live MJPEG video.
- Ollama-backed AI command box.
- Visible drive panel.
- WASD/QE keyboard movement.
- Locomotion mode buttons.
- LiDAR switch-on, LiDAR subscription, and Three.js point-cloud rendering.

Use this only after video/control and LiDAR work separately.

## Locomotion Modes

The WASD modes include locomotion buttons. Switching modes first sends `stop`, then sends the required sport commands with a small settle delay. WASD movement continues afterward through the standard `Move` command.

| Button | Sport command behavior |
| --- | --- |
| `Normal` | Attempts to disable stunt/gait toggles, then sends `BalanceStand`. |
| `Hind Walk` | Enables `WalkUpright`. This is the likely path for walking on rear legs if the firmware supports it. |
| `BackStand` | Enables upright setup and attempts `BackStand`. |
| `HandStand` | Enables `HandStand`. |
| `Bound` | Enables `FreeBound`. |
| `Jump` | Enables `FreeJump`. |
| `Classic` | Enables `ClassicWalk`. |
| `CrossStep` | Enables `CrossStep`. |
| `FreeWalk` | Sends `FreeWalk`. |
| `StaticWalk` | Sends `StaticWalk`. |
| `TrotRun` | Sends `TrotRun`. |
| `Economic` | Sends `EconomicGait`. |

Recommended test order:

```text
Normal
Balance/stand visually stable
Hind Walk
Tap W briefly
Tap Q/E briefly
Normal
```

For handstand, bound, and jump modes, test tiny taps first. Some firmware modes are posture or gait toggles, while others may be one-shot actions. If a mode is one-shot, WASD may not do anything useful until the robot returns to a normal/balanced posture.

## AI Command Path

AI modes use `LocalRobotBrain` from `src/go2_local_brain/brain/local_llm.py`.

Flow:

1. Browser posts the prompt to `/api/ai`.
2. `LocalRobotBrain` sends the prompt and tool schema to Ollama.
3. Ollama returns a native tool call.
4. The tool handler calls `Go2WebRTCClient`.
5. `Go2WebRTCClient` publishes the sport command over WebRTC.

Good first prompts:

```text
balance
stand up
turn right 90 degrees
walk forward then stop
stand on your hind legs
do a handstand
```

## Movement Path

WASD modes send repeated short `/api/move` requests while a key or button is held. The backend calls:

```python
Go2WebRTCClient.move(vx, vy, vyaw, duration_s)
```

The driver clamps speed using `src/go2_local_brain/safety/limits.py` and sends a trailing stop after each short move window. This makes keyboard movement more forgiving when a key-up event is missed.

## LiDAR Path

LiDAR modes do this after WebRTC connects:

1. Disable traffic saving if the installed SDK exposes that call.
2. Set decoder type to `libvoxel` if available.
3. Publish `on` to `rt/utlidar/switch`.
4. Subscribe to `rt/utlidar/voxel_map`.
5. Subscribe to `rt/utlidar/voxel_map_compressed`.
6. Convert decoded `positions` into flat XYZ points.
7. Downsample and stream points to the browser over WebSocket.

The HUD values mean:

```text
raw=<callbacks> rendered=<clouds> parseErrors=<bad-shape> dropped=<throttled>
```

- `raw=0`: no LiDAR callbacks are arriving.
- `raw>0 parseErrors>0`: messages are arriving but the decoded shape is not what the renderer expects.
- `raw>0 rendered>0`: backend has point clouds; if the graph looks blank, it is likely framing/orientation/rendering.

## Testing Without Hardware

These checks do not require the robot:

```bash
python -m compileall -q src
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
```

On a machine without repo dependencies installed, the full test suite may fail at import time for packages like `aiohttp` or `ollama`. In the project venv, install dependencies first:

```bash
source .venv/bin/activate
pip install -e .
```

## Upgrade And Pull

On the WSL instance or Jetson:

```bash
cd ~/robotics/go2_local_brain
git status
git pull
source .venv/bin/activate
pip install -e .
python scripts/smoke_test_imports.py
```

If local changes block `git pull` and you do not care about them:

```bash
git fetch origin
git reset --hard origin/main
source .venv/bin/activate
pip install -e .
```

Only use `git reset --hard` when you are sure the local changes are disposable.
