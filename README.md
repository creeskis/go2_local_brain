# go2_local_brain

A small, single-process Python brain for a **Unitree Go2 Air**. It turns typed natural-language commands into one bounded robot tool call at a time using **Ollama**, then sends motion and sport/action commands over **WebRTC** through `unitree_webrtc_connect`.

This repo is organized for Cooper's known setup first:

1. Installation guide for a **WSL instance**.
2. Installation guide for the **Jetson Orin Nano**.
3. Reference docs for how the driver, Ollama brain, motion tools, actions, and exploration work.

Keep this repo private. It contains operational details for a real robot.

## Known Target Setup

- Robot: Unitree Go2 Air
- Firmware: `1.1.7`
- Robot is RoboVerse-jailbroken with SSH/port 22 open
- Custom package for firmware `1.1.7` installed
- Robot STA/private-network control IP: `192.168.123.121`
- Secondary/reachable Go2 IP observed: `192.168.123.161`
- Jetson Orin target IP: `192.168.123.18`
- WSL-host machine IP on the robot subnet: `192.168.123.14`
- Jetson OS target: JetPack `6.2.1`
- Production Ollama location: local on the Jetson Orin
- Production model: `qwen3:1.7b`
- AES key: optional/blank for firmware `1.1.7` unless WebRTC auth later proves otherwise

The most important config rule:

```env
GO2_IP=192.168.123.121
```

`GO2_IP` points at the dog, not the Jetson.

# Installation Guide 1: WSL Instance

Use this guide to run and test the project from a WSL instance before deploying it to the Jetson.

## 1. Configure WSL Networking

Recommended Windows `%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
memory=24GB
processors=8
```

After changing `.wslconfig`, restart WSL from PowerShell:

```powershell
wsl --shutdown
```

Reopen the WSL instance.

## 2. Confirm Network Reachability

From the WSL instance:

```bash
ping -c 3 192.168.123.121
ping -c 3 192.168.123.18
```

Expected:

- `192.168.123.121` responds: the dog is reachable.
- `192.168.123.18` responds: the Jetson is reachable.

If `192.168.123.121` does not respond, fix networking before continuing.

## 3. Install System Packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl portaudio19-dev
```

`portaudio19-dev` is needed because `unitree_webrtc_connect` pulls in `pyaudio`, which often builds from source on Linux.

## 4. Clone The Repo

```bash
cd ~
mkdir -p robotics
cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
```

If GitHub asks for credentials over HTTPS, use your GitHub username and a GitHub personal access token.

## 5. Create The Python Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

## 6. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Recommended WSL-local test config:

```env
GO2_IP=192.168.123.121
GO2_AES_128_KEY=
OLLAMA_MODEL=qwen3:1.7b
# OLLAMA_HOST=
# FORCE_MOTION_MODE=
# ENABLE_EXPLORATION=1
# EXPLORATION_MIN_OBSTACLE_M=0.35
```

If the Python app runs in the WSL instance but Ollama runs somewhere else, set `OLLAMA_HOST`:

```env
OLLAMA_HOST=http://<ollama-host-ip>:11434
```

If Ollama also runs inside this same WSL instance, leave `OLLAMA_HOST` unset.

## 7. Install Ollama In The WSL Instance

Skip this if Ollama already runs somewhere else and you plan to use `OLLAMA_HOST`.

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama --version
ollama list
curl http://localhost:11434/api/tags
ollama pull qwen3:1.7b
ollama list
```

## 8. Run Tests

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
```

A clean run should end with:

```text
OK
```

## 9. Run The App From WSL

Only run this when you are ready for the app to connect to the dog:

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
python -m go2_local_brain.main
```

The prompt should appear:

```text
Go2 local brain ready. Type a command, or 'quit' to exit.
go2>
```

First commands:

```text
stop
stand up
balance
sit down
```

Then, in a clear area:

```text
walk forward
walk and turn left
strafe right
turn left
greet
dance
stop
```

Exploration is disabled by default. Only try it after `range_obstacle` telemetry is nonzero and believable:

```bash
nano .env
```

```env
ENABLE_EXPLORATION=1
EXPLORATION_MIN_OBSTACLE_M=0.35
```

Then:

```bash
source .venv/bin/activate
python -m go2_local_brain.main
```

```text
explore for three seconds
stop
```

## 10. If Commands Are Ignored

If WebRTC connects but sport commands appear to do nothing, edit `.env`:

```bash
nano .env
```

Set:

```env
FORCE_MOTION_MODE=normal
```

Then rerun:

```bash
source .venv/bin/activate
python -m go2_local_brain.main
```

# Installation Guide 2: Jetson Orin Nano

Use this guide for the final Jetson Orin Nano deployment. The intended production setup is app + Ollama both running locally on the Jetson.

## 1. Confirm Jetson Basics

On the Jetson:

```bash
python3 --version
uname -a
cat /etc/os-release
```

JetPack `6.2.1` is Ubuntu 22.04 based and commonly has Python 3.10. This project supports Python `>=3.10`.

## 2. Confirm Network Reachability

From the Jetson:

```bash
ping -c 3 192.168.123.121
ping -c 3 192.168.123.161
```

Expected:

- `192.168.123.121` responds: this is the dog STA/control IP.
- `192.168.123.161` may also respond, but do not use it as the default `GO2_IP` unless testing proves otherwise.

## 3. Install System Packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl portaudio19-dev
```

## 4. Clone The Repo

```bash
cd ~
mkdir -p robotics
cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
```

## 5. Create The Python Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

## 6. Configure `.env`

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
# ENABLE_EXPLORATION=1
# EXPLORATION_MIN_OBSTACLE_M=0.35
```

Leave `OLLAMA_HOST` unset because Ollama is intended to run locally on the Jetson.

## 7. Install Ollama On Jetson

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama --version
ollama list
curl http://localhost:11434/api/tags
ollama pull qwen3:1.7b
ollama list
```

## 8. Run Tests On Jetson

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
```

A clean run should end with:

```text
OK
```

## 9. Run The App On Jetson

Only run this when you are ready for the app to connect to the dog:

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
python -m go2_local_brain.main
```

First commands:

```text
stop
stand up
balance
sit down
```

Then, in a clear area:

```text
walk forward
walk and turn left
strafe right
turn left
greet
dance
jump
pounce
stop
```

`jump` and `pounce` depend on which advanced `SPORT_CMD` names your installed `unitree_webrtc_connect` package exposes. If the command is missing, the app stops and prints a tool failure instead of guessing.

## 10. Optional Motion Mode Diagnostic

If the app connects but movement is ignored, edit `.env`:

```bash
nano .env
```

Set:

```env
FORCE_MOTION_MODE=normal
```

Rerun:

```bash
source .venv/bin/activate
python -m go2_local_brain.main
```

The driver will query the robot's motion mode, switch to `normal` if needed, and wait about 5 seconds before accepting commands.

# Reference: How The App Works

## Architecture

The app is intentionally one process:

```text
User prompt
  -> LocalRobotBrain
  -> Ollama chat with tool schemas
  -> exactly one robot tool call
  -> Go2WebRTCClient
  -> unitree_webrtc_connect WebRTC data channel
  -> Go2 Air
```

Important files:

```text
src/go2_local_brain/main.py                    Entry point and REPL wiring
src/go2_local_brain/config.py                  .env/environment loader
src/go2_local_brain/brain/local_llm.py          Ollama tool-calling brain
src/go2_local_brain/driver/webrtc_client.py     Unitree WebRTC driver wrapper
src/go2_local_brain/safety/limits.py            Motion caps and deadman timeout
scripts/smoke_test_imports.py                   Import/package smoke test
tests/test_brain.py                             Brain tests
tests/test_driver.py                            Driver tests
```

## Confirmed WebRTC Behavior

The live run connected successfully to `192.168.123.121` using LAN signaling:

```text
LAN Signaling Method : con_notify (192.168.123.121:9991)
Data Channel Verification: OK
Go2 WebRTC connected at 192.168.123.121
```

The ICE log showed several failed candidates, then a successful IPv6 peer reflexive candidate. That is fine; WebRTC tries candidates until one works.

The sport-state stream arrived on `rt/lf/sportmodestate`, matching upstream `LF_SPORT_MOD_STATE` examples.

## Brain Layer

`LocalRobotBrain` sends every prompt to Ollama with a compact system prompt and tool schemas. It deliberately executes only the first tool call. Multi-step plans are out of scope for this small local brain; the operator can issue another prompt for the next action.

If Ollama fails, returns no tool call, returns an unknown tool, or returns bad arguments, the brain calls `stop()`.

Current tools:

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
robot_walk_turn
robot_move
robot_greet
robot_dance
robot_jump
robot_pounce
robot_stretch
robot_wiggle
robot_explore_room
```

Good prompt examples:

```text
stand up
balance
walk forward
back up
strafe left
turn right
walk forward while turning left
greet
dance
jump
pounce
stretch
explore for three seconds
stop
sit down
```

## Driver Layer

`Go2WebRTCClient` wraps `unitree_webrtc_connect`.

The app uses the 2.x API shape:

```python
conn.datachannel.pub_sub
```

Sport requests are sent to:

```python
RTC_TOPIC["SPORT_MOD"]
```

The confirmed `Move` payload is:

```json
{"x": 0.2, "y": 0.0, "z": 0.0}
```

Meaning:

- `x`: forward velocity in m/s
- `y`: lateral velocity in m/s, positive left
- `z`: yaw rate in rad/s, positive counter-clockwise

Walking and turning at the same time is just one `Move` payload with both `x` and `z` nonzero.

## Expanded Motion Limits

Limits live in `src/go2_local_brain/safety/limits.py`:

```python
MAX_VX = 0.75
MAX_VY = 0.40
MAX_VYAW = 1.10
DEFAULT_MOVE_DURATION_S = 0.45
MAX_MOVE_DURATION_S = 2.0
DEADMAN_TIMEOUT_S = 0.75
```

The driver clamps velocities. The brain rejects non-finite values and caps duration before the command reaches the driver.

Do not rely on the model for safety. The model proposes; the driver enforces.

## Advanced Actions

The driver exposes named actions through `advanced_action()` and maps them to the first available upstream `SPORT_CMD` candidate:

```text
greet   -> Hello
dance   -> Dance1, Dance2, WiggleHips
jump    -> FrontJump, FreeJump
pounce  -> FrontPounce, Pounce
stretch -> Stretch
wiggle  -> WiggleHips
```

If a command is not present in the installed `unitree_webrtc_connect` package, the action raises an error, the brain calls `stop()`, and the REPL reports the failure.

## Exploration

`robot_explore_room` runs short forward/turn steps for a bounded time. It is intentionally opt-in:

```env
ENABLE_EXPLORATION=1
EXPLORATION_MIN_OBSTACLE_M=0.35
```

It refuses to run when:

- `ENABLE_EXPLORATION` is not set.
- Sport-state telemetry is missing or stale.
- `range_obstacle` is missing.
- `range_obstacle` is all zeros.

Your live log showed:

```text
range_obstacle=[0,0,0,0]
```

That is treated as unavailable, not clear space. Exploration will refuse blind movement until telemetry is actually useful.

## StandUp And BalanceStand

Firmware exposes a locked stand posture and a balance stand mode. Upstream MCF examples warn that after `StandUp`, joints may be locked until `BalanceStand` is called.

So `robot_stand_up` does:

```text
StandUp -> wait 2.5s -> BalanceStand
```

This is why a single `stand up` prompt should leave the dog ready to accept normal `Move` commands.

## Motion Mode

Firmware `1.1.7` supports MCF-related sport examples upstream. The current core commands have matching IDs in normal and MCF tables:

```text
BalanceStand = 1002
StopMove     = 1003
StandUp      = 1004
StandDown    = 1005
Move         = 1008
Sit          = 1009
```

If commands are ignored, use:

```env
FORCE_MOTION_MODE=normal
```

That enables a startup pre-flight that mirrors upstream `sportmode.py`: query `MOTION_SWITCHER`, switch if needed, then wait for the controller to settle.

# Reference: Environment Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `GO2_IP` | `192.168.123.121` | Dog WebRTC/control endpoint |
| `GO2_AES_128_KEY` | blank | Optional AES key for newer firmware/auth cases |
| `OLLAMA_MODEL` | `qwen3:1.7b` | Ollama model name |
| `OLLAMA_HOST` | unset | Only set when Ollama is remote from the app |
| `FORCE_MOTION_MODE` | unset | Optional motion-mode switch, usually `normal` |
| `ENABLE_EXPLORATION` | false | Enables telemetry-gated exploration |
| `EXPLORATION_MIN_OBSTACLE_M` | `0.35` | Minimum obstacle distance for exploration |
| `VERBOSE_WEBRTC_LOGS` | false | Restores upstream INFO packet logs |

# Reference: Model Choices

Default:

```env
OLLAMA_MODEL=qwen3:1.7b
```

This is the right default for Jetson Orin Nano because the live brain now presents simple named tools. The model usually only needs to choose `robot_dance`, `robot_step_forward`, `robot_walk_turn`, etc., instead of inventing raw command sequences.

For stronger WSL-instance testing, you can try:

```env
OLLAMA_MODEL=qwen3:8b
```

For the American-model side project, evaluate models with:

```bash
python scripts/eval_model_tools.py --model <model-name>
```

See `docs/AMERICAN_MODEL_EVAL.md` for the current evaluation plan.

# Reference: Upgrading

## Upgrade On A WSL Instance

```bash
cd ~/robotics/go2_local_brain
git status
git pull
source .venv/bin/activate
pip install -e .
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
```

If `.env.example` changed, compare it with your local `.env`:

```bash
diff -u .env.example .env
```

Do not overwrite `.env` blindly.

## Upgrade On Jetson

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

# Reference: Troubleshooting

## Robot Does Not Connect

```bash
ping -c 3 192.168.123.121
cat .env
```

Check:

- `GO2_IP` is `192.168.123.121`, not `192.168.123.18`.
- The WSL instance or Jetson can route to `192.168.123.x`.
- Another client is not already connected over WebRTC.
- AES key is not unexpectedly required.

## App Connects But Movement Is Ignored

Set:

```env
FORCE_MOTION_MODE=normal
```

Then rerun:

```bash
source .venv/bin/activate
python -m go2_local_brain.main
```

## Ollama Fails

```bash
ollama list
curl http://localhost:11434/api/tags
```

If Ollama is remote, set:

```bash
export OLLAMA_HOST=http://<ollama-host-ip>:11434
```

## Model Returns No Tool Call

Use simple prompts:

```text
stand up
walk forward
walk and turn left
dance
stop
sit down
```

If testing on stronger hardware, try a larger model:

```env
OLLAMA_MODEL=qwen3:8b
```

## Exploration Refuses To Run

This is expected unless all three are true:

- `ENABLE_EXPLORATION=1` is set.
- Sport-state telemetry is arriving.
- `range_obstacle` is nonzero and fresh.

Run with verbose logs to inspect telemetry:

```env
VERBOSE_WEBRTC_LOGS=1
```

Then:

```bash
python -m go2_local_brain.main
```

If `range_obstacle` remains `[0,0,0,0]`, exploration is not available from that stream yet.

## pyaudio Or portaudio Build Fails

```bash
sudo apt update
sudo apt install -y portaudio19-dev
source .venv/bin/activate
pip install -e .
```

# Quick Command Summary

WSL instance:

```bash
cd ~
mkdir -p robotics
cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl portaudio19-dev
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
cp .env.example .env
ollama pull qwen3:1.7b
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
python -m go2_local_brain.main
```

Jetson Orin:

```bash
cd ~
mkdir -p robotics
cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl portaudio19-dev
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
cp .env.example .env
ollama pull qwen3:1.7b
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
python -m go2_local_brain.main
```
