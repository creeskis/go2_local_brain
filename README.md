# go2_local_brain

A small, single-process Python brain for the Unitree Go2 Air.

- Talks to the robot over local-network **WebRTC** (`unitree_webrtc_connect`).
- Uses a local **Ollama** model with native tool calling to convert typed
  prompts into one robot action at a time.
- Keeps WebRTC happy by never blocking the asyncio loop (blocking calls run
  via `asyncio.to_thread`).
- Has conservative motion caps and a deadman loop.

## Setup (WSL Linux)

### Fast path

After copying the project onto the target machine:

```bash
cd go2_local_brain
bash bootstrap.sh
```

That handles apt deps, venv, install, `.env`, and the smoke test in one go.

### Manual path

```bash
# 1. System packages
#   portaudio19-dev is required because unitree_webrtc_connect's pyaudio
#   dependency builds from source on Linux.
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git portaudio19-dev

# 2. Project location
cd ~
mkdir -p robotics
cd robotics
# (if you haven't already created/cloned the project, do so now)
cd go2_local_brain

# 3. Virtualenv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

# 4. Install the package + deps
pip install -e .

# 5. Configure
cp .env.example .env
# edit .env to set GO2_IP, optional GO2_AES_128_KEY, OLLAMA_MODEL

# 6. Pull the local model
ollama pull qwen3:1.7b

# 7. Smoke test
python scripts/smoke_test_imports.py

# 8. Run the brain (only when you actually want to drive the robot)
python -m go2_local_brain.main
```

## Jetson Orin Nano (JetPack 6.2.1)

JetPack 6 ships Python 3.10 on Ubuntu 22.04. `pyproject.toml` is
`requires-python = ">=3.10"` so the default JetPack Python works as-is.
If you have 3.11+ available, use it - anything 3.10+ is fine.

If `python -m go2_local_brain.main` connects but the dog ignores motion
commands, the controller may be in MCF or AI mode. Set
`FORCE_MOTION_MODE=normal` in `.env` and re-run; the driver will switch
the controller into "normal" sport mode at connect time (mirrors what
upstream's `examples/go2/data_channel/sportmode/sportmode.py` does).

## Model choice

The default `OLLAMA_MODEL=qwen3:1.7b` targets a **Jetson Orin Nano**: it
fits in memory and runs the simple one-tool-per-prompt loop adequately.
The system prompt and validation layer are written for a small model
(strict schemas, short examples, driver-side clamps).

For offboard / laptop planners with more RAM, you'll get better tool-call
reliability from:

- `qwen3:8b` - what DimensionOS uses for its `unitree-go2-agentic-ollama`
  blueprint. ~5 GB.
- `lfm2.5:8b` - 8B model tuned for tool calling, similar footprint.
- `gpt-oss:20b` - better planning, ~14 GB; slower per turn.

Set via `OLLAMA_MODEL` in `.env`. The safety layer is identical regardless
of model: clamps, duration cap, deadman, and finite-arg validation all
live in the driver / brain - never trust the planner.

## Tool calls the brain can pick

- `robot_stand_up`
- `robot_sit_down`
- `robot_stop`
- `robot_move(vx, vy=0, vyaw=0, duration_s=0.35)`

Everything is clamped to the limits in `safety/limits.py`. If the model
returns no tool call or an unknown one, the robot is told to **stop**.

## Troubleshooting

- **WSL can't reach the robot.** Check `ping $GO2_IP` from inside WSL.
  With WSL2 **mirrored networking** (`[wsl2] networkingMode=mirrored` in
  `%USERPROFILE%/.wslconfig`) the Windows host's interfaces are shared
  directly with WSL, so the `192.168.123.x` Go2 subnet is normally
  reachable. Without mirrored networking, WSL2's default NAT often
  cannot see that subnet - you'll need either mirrored mode or a
  USB-Ethernet adapter bridged in, or run the brain on the Windows host
  directly.
- **Where is Ollama?** Only set `OLLAMA_HOST` when the brain and the
  Ollama server are in *different* environments.
  - Jetson-local production (brain + Ollama both on Jetson): leave unset.
  - WSL app + WSL Ollama: leave unset.
  - WSL app + Windows-host Ollama: `export OLLAMA_HOST=http://<host-ip>:11434`
    (try the host IP from `ip route | awk '/^default/ {print $3}'` if you
    don't already have it).
- **`unitree_webrtc_connect` API drift.** The 2.x line on PyPI does *not*
  expose a `SportClient`; the driver here goes straight to
  `conn.datachannel.pub_sub` and uses `RTC_TOPIC["SPORT_MOD"]` +
  `SPORT_CMD` ids. If a future release renames `datachannel` or `pub_sub`,
  inspect with `python -c "import unitree_webrtc_connect as u; print(dir(u))"`
  and adjust `_find_pubsub()` in `driver/webrtc_client.py`.
- **`pyaudio` build fails (`portaudio.h: No such file or directory`).**
  Install `portaudio19-dev` as shown above. If you genuinely don't want
  audio support, `pip install --no-deps unitree_webrtc_connect` plus the
  rest of its non-audio deps will skip `pyaudio` entirely.
- **Robot firmware requires the AES key.** Set `GO2_AES_128_KEY` in `.env`.
  Without it, newer firmware refuses the WebRTC handshake.
- **Deadman keeps stopping the robot mid-command.** That means the brain
  isn't refreshing fast enough - either the LLM call is slow or the network
  is dropping frames. Increase `DEADMAN_TIMEOUT_S` in `safety/limits.py`
  only after you've understood why.
