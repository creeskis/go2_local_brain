# go2_local_brain

A single-process Python brain for a **Unitree Go2 Air**. It turns typed natural-language commands into robot tool calls using **Ollama**, then sends motion and sport/action commands over **WebRTC** through `unitree_webrtc_connect`.

This repo is tuned for Cooper's known setup:

- Robot: Unitree Go2 Air, firmware `1.1.7`
- Robot STA/control IP: `192.168.123.121`
- Secondary reachable Go2 IP observed: `192.168.123.161`
- Jetson Orin target IP: `192.168.123.18`, JetPack `6.2.1`
- WSL-host machine IP on the robot subnet: `192.168.123.14`
- Production model: `qwen3:1.7b` through Ollama
- AES key: blank unless WebRTC auth later proves otherwise

The important rule:

```env
GO2_IP=192.168.123.121
```

`GO2_IP` points at the dog, not the Jetson.

# Installation Guide 1: WSL Instance

## 1. Configure WSL Networking

Recommended Windows `%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
memory=24GB
processors=8
```

Restart WSL from PowerShell:

```powershell
wsl --shutdown
```

## 2. Confirm Reachability

```bash
ping -c 3 192.168.123.121
ping -c 3 192.168.123.18
```

## 3. Install And Clone

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl portaudio19-dev
cd ~
mkdir -p robotics
cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

## 4. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Base config:

```env
GO2_IP=192.168.123.121
GO2_AES_128_KEY=
OLLAMA_MODEL=qwen3:1.7b
# OLLAMA_HOST=
# FORCE_MOTION_MODE=
```

Exploration config:

```env
ENABLE_EXPLORATION=1
EXPLORATION_MODE=telemetry
EXPLORATION_MIN_OBSTACLE_M=0.35
EXPLORATION_MAX_DURATION_S=15
```

`EXPLORATION_MODE` options:

- `telemetry`: require fresh nonzero `range_obstacle`.
- `relaxed`: use `range_obstacle` if available; otherwise roam in small arcs.
- `blind`: ignore `range_obstacle` entirely. Use only in a clear area.

## 5. Install Ollama In The WSL Instance

Skip this if Ollama runs somewhere else and you set `OLLAMA_HOST`.

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:1.7b
ollama list
curl http://localhost:11434/api/tags
```

## 6. Test And Run

```bash
source .venv/bin/activate
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
python -m go2_local_brain.main
```

# Installation Guide 2: Jetson Orin Nano

## 1. Confirm Basics

```bash
python3 --version
uname -a
cat /etc/os-release
ping -c 3 192.168.123.121
```

## 2. Install And Clone

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl portaudio19-dev
cd ~
mkdir -p robotics
cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

## 3. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Recommended Jetson config:

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

Leave `OLLAMA_HOST` unset when Ollama runs locally on the Jetson.

## 4. Install Ollama And Run

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:1.7b
source .venv/bin/activate
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
python -m go2_local_brain.main
```

# What The Dog Can Do Now

For the combined browser controller with manual buttons, AI commands, live video, and LiDAR, see:

```bash
python -m go2_local_brain.gui --host 0.0.0.0 --port 8765
```

Details: `docs/unified_gui.md`.

For a separate manual-only cockpit with live video, WASD/QE controls, and exact legion1581 sport-command buttons:

```bash
python -m go2_local_brain.control_gui --host 0.0.0.0 --port 8770
```

Open `http://localhost:8770`. This GUI does not start AI or LiDAR.

Good startup prompts:

```text
stop
stand up
balance
sit down
```

Movement prompts:

```text
walk forward
back up
strafe left
turn right
walk forward while turning left
turn around
turn 180 degrees right
```

Linked commands:

```text
walk forward then turn right
walk forward, strafe left, then turn around
make a loop: forward, turn left, forward, turn left
```

Dance/action prompts:

```text
greet
dance
make up a dance
do a spin dance
do a sway dance
jump
pounce
stretch
wiggle
```

Exploration prompts:

```text
what telemetry do you see
explore for five seconds
explore in relaxed mode for ten seconds
explore even without telemetry for eight seconds
stop
```

The brain executes exactly one Ollama tool call per prompt, but `robot_sequence` lets that one tool call contain up to eight linked steps.

# Tool List

The Ollama model can choose from:

```text
robot_stand_up
robot_balance_stand
robot_recovery_stand
robot_sit_down
robot_stop
robot_step_forward
robot_step_back
robot_strafe_left
robot_strafe_right
robot_turn_left
robot_turn_right
robot_turn_180
robot_walk_turn
robot_sequence
robot_move
robot_greet
robot_dance
robot_dance_move
robot_jump
robot_pounce
robot_stretch
robot_wiggle
robot_explore_room
robot_telemetry_report
```

# Telemetry Notes

The live run proved WebRTC connects and receives `rt/lf/sportmodestate`. That stream included pose, IMU, mode, gait, velocity, and `range_obstacle`, but `range_obstacle` was `[0,0,0,0]`.

Upstream `unitree_webrtc_connect` documents several separate telemetry/control surfaces for Go2:

- `data_channel/sportmodestate/`: LF sport mode state.
- `data_channel/lowstate/`: lower-level state.
- `data_channel/multiplestate/`: multiple state topics.
- `data_channel/lidar/lidar_stream.py`: LiDAR point cloud stream.
- Obstacle avoidance API support.

So all-zero `range_obstacle` probably means that field is not populated or obstacle sensing is not enabled in this sport-state stream on this setup. It does not prove LiDAR or every obstacle API is unavailable.

Use:

```text
what telemetry do you see
```

For deeper diagnosis, next repo work should add explicit lowstate, multiplestate, LiDAR, and obstacle-avoidance API diagnostics.

# Hind-Feet Walking Goal

The current repo now exposes the path toward that goal:

1. Confirm which advanced `SPORT_CMD` actions exist on the installed package.
2. Test balance, recovery stand, backstand/handstand style actions if exposed.
3. Build movement macros around stable postures.
4. Only then attempt hind-feet behavior.

If the firmware exposes `BackStand`, `Handstand`, or related action names, the driver already has candidate mappings for advanced actions. If not, true hind-feet walking will need either a specific firmware action, a custom motion package, or lower-level control beyond this WebRTC sport-mode wrapper.

# Troubleshooting

If commands are ignored:

```env
FORCE_MOTION_MODE=normal
```

If exploration refuses:

```env
ENABLE_EXPLORATION=1
EXPLORATION_MODE=relaxed
```

If you explicitly want no obstacle telemetry gate:

```env
ENABLE_EXPLORATION=1
EXPLORATION_MODE=blind
```

If logs are too quiet while diagnosing telemetry:

```env
VERBOSE_WEBRTC_LOGS=1
```

Then run:

```bash
source .venv/bin/activate
python -m go2_local_brain.main
```

# Upgrade Commands

WSL instance or Jetson:

```bash
cd ~/robotics/go2_local_brain
git status
git pull
source .venv/bin/activate
pip install -e .
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
python -m go2_local_brain.main
```
