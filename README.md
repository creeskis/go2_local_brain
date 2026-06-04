# go2_local_brain

A small, single-process Python brain for a **Unitree Go2 Air**. It turns typed natural-language commands into one safe robot tool call at a time using **Ollama**, then sends bounded motion commands over **WebRTC** through `unitree_webrtc_connect`.

This README is split into two parts:

1. Installation guide for a **WSL instance**.
2. Installation guide for the **Jetson Orin Nano**.
3. Reference sections explaining how the app works, what to change, and how to troubleshoot it.

Keep this repo private. It contains operational details about a real robot setup.

## Known Target Setup

- Robot: Unitree Go2 Air
- Firmware: `1.1.7`
- Robot is RoboVerse-jailbroken with SSH/port 22 open
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

Use this for WSL-local Ollama testing:

```env
GO2_IP=192.168.123.121
GO2_AES_128_KEY=
OLLAMA_MODEL=qwen3:1.7b
# OLLAMA_HOST=
# FORCE_MOTION_MODE=
```

If the Python app runs in WSL but Ollama runs somewhere else, set `OLLAMA_HOST`:

```env
OLLAMA_HOST=http://<ollama-host-ip>:11434
```

If Ollama also runs inside this same WSL instance, leave `OLLAMA_HOST` unset.

## 7. Install Ollama In The WSL Instance

Skip this section if Ollama already runs somewhere else and you plan to use `OLLAMA_HOST`.

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Start or verify Ollama:

```bash
ollama --version
ollama list
curl http://localhost:11434/api/tags
```

Pull the default model:

```bash
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

First commands to try:

```text
stop
stand up
sit down
```

Only after stationary commands look good, test tiny movement in a clear area:

```text
move forward a tiny bit
turn left slowly
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

JetPack `6.2.1` is Ubuntu 22.04 based and commonly has Python 3.10. This project supports Python `>=3.10`, so the default Python should be fine.

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

Use this Jetson target config:

```env
GO2_IP=192.168.123.121
GO2_AES_128_KEY=
OLLAMA_MODEL=qwen3:1.7b
# OLLAMA_HOST=
# FORCE_MOTION_MODE=
```

Leave `OLLAMA_HOST` unset because Ollama is intended to run locally on the Jetson.

## 7. Install Ollama On Jetson

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Verify Ollama:

```bash
ollama --version
ollama list
curl http://localhost:11434/api/tags
```

Pull the Jetson default model:

```bash
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
sit down
```

Then, in a clear area:

```text
move forward a tiny bit
turn left slowly
stop
```

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
src/go2_local_brain/viz/rerun_logger.py         Placeholder Rerun logger
scripts/smoke_test_imports.py                   Import/package smoke test
tests/test_brain.py                             Brain tests
tests/test_driver.py                            Driver tests
```

## Brain Layer

`LocalRobotBrain` sends every prompt to Ollama with a compact system prompt and four tool schemas:

- `robot_stand_up`
- `robot_sit_down`
- `robot_stop`
- `robot_move`

It deliberately executes only the first tool call. Multi-step plans are out of scope for this small local brain. The operator can issue another prompt for the next action.

If Ollama fails, returns no tool call, returns an unknown tool, or returns bad arguments, the brain calls `stop()`.

## Driver Layer

`Go2WebRTCClient` wraps `unitree_webrtc_connect`.

The app uses the 2.x API shape:

```python
conn.datachannel.pub_sub
```

There is no `SportClient` in this package version.

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

## Safety Layer

Limits live in `src/go2_local_brain/safety/limits.py`:

```python
MAX_VX = 0.35
MAX_VY = 0.20
MAX_VYAW = 0.45
DEFAULT_MOVE_DURATION_S = 0.35
MAX_MOVE_DURATION_S = 1.0
DEADMAN_TIMEOUT_S = 0.75
```

The driver clamps velocities. The brain rejects non-finite values and caps duration before the command reaches the driver.

Do not rely on the model for safety. The model proposes; the driver enforces.

## StandUp And BalanceStand

Firmware exposes a locked stand posture and a balance stand mode. Upstream MCF examples warn that after `StandUp`, joints may be locked until `BalanceStand` is called.

So `robot_stand_up` does:

```text
StandUp -> wait 2.5s -> BalanceStand
```

This is why a single `stand up` prompt should leave the dog ready to accept normal `Move` commands.

## Motion Mode

Firmware `1.1.7` supports MCF-related sport examples upstream. The current commands have matching IDs in normal and MCF tables:

```text
BalanceStand = 1002
StopMove     = 1003
StandUp      = 1004
StandDown    = 1005
Move         = 1008
Sit          = 1009
```

Because these IDs match, the app does not currently need a command-table switch.

If commands are ignored, use:

```env
FORCE_MOTION_MODE=normal
```

That enables a startup pre-flight that mirrors upstream `sportmode.py`: query `MOTION_SWITCHER`, switch if needed, then wait for the controller to settle.

## Sport State Telemetry

The driver subscribes passively to sport-state telemetry. It logs mode/gait transitions but does not yet block motion based on telemetry.

Reason: the exact schema and enum values should be confirmed on this dog before making movement gates. False blocking during bring-up would make debugging harder.

Future improvement: after hardware confirmation, refuse `move()` when posture/mode/fault state is unsafe.

# Reference: Environment Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `GO2_IP` | `192.168.123.121` | Dog WebRTC/control endpoint |
| `GO2_AES_128_KEY` | blank | Optional AES key for newer firmware/auth cases |
| `OLLAMA_MODEL` | `qwen3:1.7b` | Ollama model name |
| `OLLAMA_HOST` | unset | Only set when Ollama is remote from the app |
| `FORCE_MOTION_MODE` | unset | Optional motion-mode switch, usually `normal` |

# Reference: Model Choices

## Default Model

```env
OLLAMA_MODEL=qwen3:1.7b
```

This is the right default for Jetson Orin Nano because the live robot brain only needs small, bounded tool choices.

## Larger Optional Models

For an offboard planner or stronger WSL instance test:

```env
OLLAMA_MODEL=qwen3:8b
```

or:

```env
OLLAMA_MODEL=lfm2.5:8b
```

For slower planning with more memory:

```env
OLLAMA_MODEL=gpt-oss:20b
```

The safety layer does not change with the model.

# Reference: Upgrading

## Upgrade On A WSL Instance

```bash
cd ~/robotics/go2_local_brain
git status
git pull
source .venv/bin/activate
pip install -e .
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
python -m unittest discover -s tests
```

Then run:

```bash
python -m go2_local_brain.main
```

# Reference: What To Change

## Safe Changes

- Add tests under `tests/`.
- Add more logging.
- Tune examples in `src/go2_local_brain/brain/local_llm.py`.
- Change `OLLAMA_MODEL` in `.env`.
- Set `FORCE_MOTION_MODE=normal` during diagnostics.
- Add passive sport-state fields for logging.

## Hardware-Sensitive Changes

Be careful with:

- Raising `MAX_VX`, `MAX_VY`, or `MAX_VYAW`.
- Raising `MAX_MOVE_DURATION_S`.
- Raising `DEADMAN_TIMEOUT_S`.
- Automatically forcing motion mode on every startup.
- Blocking movement based on sport-state telemetry before confirming schema.
- Adding dynamic/advanced MCF actions.

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
move forward a tiny bit
turn left slowly
stop
sit down
```

If testing on stronger hardware, try a larger model:

```env
OLLAMA_MODEL=qwen3:8b
```

## pyaudio Or portaudio Build Fails

```bash
sudo apt update
sudo apt install -y portaudio19-dev
source .venv/bin/activate
pip install -e .
```

## WSL Instance Cannot Reach Robot Subnet

Confirm `.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
memory=24GB
processors=8
```

Restart WSL:

```powershell
wsl --shutdown
```

Then retry:

```bash
ping -c 3 192.168.123.121
```

# Future Work

Good next improvements:

- Add a direct diagnostic command path that bypasses Ollama for `stand`, `balance`, `tiny move`, and `stop`.
- Add `robot_balance_stand`.
- Add `robot_recovery_stand`.
- Add a higher-level `relative_move(forward_m, left_m, yaw_deg)` tool.
- Add telemetry-driven movement gates after confirming sport-state schema.
- Add a DimensionOS-compatible blueprint wrapper.
- Add structured first-run logs.
- Add optional checks against upstream `sportmode.py`, `sportmode_mcf.py`, and `sportmodestate.py`.

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
