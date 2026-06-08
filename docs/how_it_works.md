# How The Project Works

`go2_local_brain` is a single-process asyncio app around three ideas:

1. Keep one WebRTC connection to the Unitree Go2.
2. Expose a small set of safe Python methods for robot movement/actions.
3. Let either a human UI or Ollama tool calls invoke those methods.

## Main Files

| File | Purpose |
| --- | --- |
| `src/go2_local_brain/main.py` | Terminal AI REPL. No browser UI. |
| `src/go2_local_brain/brain/local_llm.py` | Ollama chat/tool-calling layer. Defines the robot tools the model can call. |
| `src/go2_local_brain/driver/webrtc_client.py` | WebRTC driver wrapper. Publishes movement and sport/action commands. |
| `src/go2_local_brain/safety/limits.py` | Speed caps, default move duration, and deadman timing. |
| `src/go2_local_brain/viewer.py` | Standalone video/LiDAR viewer helpers and renderer. |
| `src/go2_local_brain/mode_gui.py` | Shared implementation for the feature-specific browser GUIs. |
| `src/go2_local_brain/control_gui.py` | Manual cockpit with exact sport-command buttons. |
| `src/go2_local_brain/gui.py` | Older all-in-one browser GUI. |

## Configuration

Configuration is loaded from `.env` by `src/go2_local_brain/config.py`.

Important values:

```env
GO2_IP=192.168.123.121
GO2_AES_128_KEY=
OLLAMA_MODEL=qwen3:1.7b
# OLLAMA_HOST=
# FORCE_MOTION_MODE=
ENABLE_EXPLORATION=1
EXPLORATION_MODE=relaxed
EXPLORATION_MIN_OBSTACLE_M=0.35
EXPLORATION_MAX_DURATION_S=15
```

Notes:

- `GO2_IP` is the robot IP, not the Jetson IP.
- Leave `GO2_AES_128_KEY` blank unless the firmware starts requiring it.
- Leave `OLLAMA_HOST` blank when Ollama runs on the same machine as the Python process.
- Set `OLLAMA_HOST=http://192.168.123.18:11434` if the Python process runs on a WSL instance and Ollama runs on the Jetson.

## WebRTC Driver Flow

`Go2WebRTCClient.connect()` does the robot-side setup:

1. Creates `UnitreeWebRTCConnection` using `WebRTCConnectionMethod.LocalSTA`.
2. Connects to `GO2_IP`.
3. Finds the pub/sub data channel.
4. Loads `RTC_TOPIC`, `SPORT_CMD`, and optionally `SPORT_CMD_MCF`.
5. Subscribes to sport-state telemetry if the topic exists.
6. Starts a deadman loop.

Movement goes through:

```python
await client.move(vx, vy, vyaw, duration_s)
```

That method:

1. Clamps `vx`, `vy`, and `vyaw` using `safety/limits.py`.
2. Starts a short move loop.
3. Re-publishes the move command at a fixed refresh rate.
4. Sends `StopMove` or zero velocity at the end.

Sport/action commands go through:

```python
await client.sport_command("WalkUpright", {"data": True})
await client.advanced_action("backstand")
```

`sport_command()` is exact-name access to installed `SPORT_CMD` / `SPORT_CMD_MCF` entries.

`advanced_action()` is a friendly alias layer for actions like:

- `greet`
- `dance`
- `jump`
- `pounce`
- `handstand`
- `backstand`
- `bound`

## Deadman Behavior

The driver keeps a background deadman loop. If no fresh command arrives within `DEADMAN_TIMEOUT_S`, it publishes zero velocity.

This is not a replacement for human supervision. It is just a guard against missed key-up events or stalled command loops.

## Ollama Tool Flow

The AI path lives in `brain/local_llm.py`.

Flow:

1. User prompt arrives from the terminal REPL or browser `/api/ai`.
2. `LocalRobotBrain.handle()` sends the prompt, system prompt, and tool schema to Ollama.
3. Ollama returns a native tool call.
4. The Python handler dispatches that tool call to `Go2WebRTCClient`.
5. The driver publishes the WebRTC sport command.

The model is expected to call tools rather than free-writing robot commands.

Examples:

```text
turn right 90 degrees
walk forward then turn left
stand on your hind legs
make up a dance
explore for ten seconds
```

Linked commands use `robot_sequence`, which accepts up to eight steps. The sequence layer normalizes common model-generated names like `robotstep_forward` back into known commands.

## Browser GUI Flow

The feature-specific browser modules are thin wrappers around `mode_gui.py`.

Each wrapper defines a `GuiMode`:

```python
GuiMode(
    title="Go2 AI + WASD + Video + LiDAR",
    enable_ai=True,
    enable_keyboard=True,
    enable_lidar=True,
    show_drive_panel=True,
)
```

`ModeGui` then conditionally starts:

- `/api/ai` when AI is enabled.
- `/api/move` and `/api/mode` when keyboard driving is enabled.
- LiDAR WebSocket messages when LiDAR is enabled.
- MJPEG video in every mode.

This makes each GUI narrow enough to test one layer at a time.

## Locomotion Modes

WASD modes have a locomotion panel for experimental firmware modes.

Mode switching does this:

1. Send `stop`.
2. Send one or more exact sport commands.
3. Wait a short settle delay between commands.
4. Record the active mode in the GUI status bar.

Important mode mappings:

```text
Hind Walk -> WalkUpright {"data": true}
BackStand -> WalkUpright {"data": true}, then BackStand {"data": true}
HandStand -> HandStand {"data": true}
Bound -> FreeBound {"data": true}
Jump -> FreeJump {"data": true}
Normal -> disable toggles, then BalanceStand
```

If a mode glitches, press `Normal`, then `STOP`, then let the robot visibly settle before trying another mode.

## Video Flow

Browser video uses the Unitree SDK video track:

1. Enable video with `switchVideoChannel(True)`.
2. Register an async frame callback.
3. Convert each frame to JPEG.
4. Serve it as MJPEG from `/video.mjpg`.

The browser uses a normal `<img src="/video.mjpg">`, so there is no custom video player.

## LiDAR Flow

LiDAR modes:

1. Publish `on` to `rt/utlidar/switch`.
2. Subscribe to both `rt/utlidar/voxel_map` and `rt/utlidar/voxel_map_compressed`.
3. Read decoded `positions` as flat XYZ triples.
4. Orient and center the points for Three.js.
5. Downsample points.
6. Send point clouds to the browser over WebSocket.

Known limitation:

- The Go2 setup produced LiDAR callbacks, but rendering still needs hardware-side verification. The HUD counters are there to separate “no callbacks,” “parse failure,” and “render/framing failure.”

## Where To Change Things

Change AI behavior:

```text
src/go2_local_brain/brain/local_llm.py
```

Change movement speed caps:

```text
src/go2_local_brain/safety/limits.py
```

Change WebRTC command publishing:

```text
src/go2_local_brain/driver/webrtc_client.py
```

Change browser mode layout or locomotion mode buttons:

```text
src/go2_local_brain/mode_gui.py
```

Change manual sport-command cockpit:

```text
src/go2_local_brain/control_gui.py
```

## Recommended Test Sequence

Without hardware:

```bash
source .venv/bin/activate
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
python -m compileall -q src
```

With hardware:

```bash
source .venv/bin/activate
python -m go2_local_brain.wasd_video_gui --host 0.0.0.0 --port 8773
```

Then:

1. Confirm video appears.
2. Press `STOP`.
3. Press `Normal`.
4. Tap `W` briefly.
5. Tap `Q` and `E` briefly.
6. Try one locomotion mode.
7. Return to `Normal`.

After that, move to:

```bash
python -m go2_local_brain.ai_wasd_lidar_gui --host 0.0.0.0 --port 8774
```

Only test the full mode after the isolated WASD/video mode behaves.
