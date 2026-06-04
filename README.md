# go2_local_brain

A small, single-process Python brain for a **Unitree Go2 Air** that runs local natural-language commands through **Ollama tool calling** and drives the robot over **WebRTC** using `unitree_webrtc_connect`.

This README is the operating guide for the project: how to clone it, run it on a WSL instance, run it on a Jetson Orin Nano, configure the Go2 Air network settings, choose an Ollama model, upgrade the code, and safely change the motion limits.

## Current Target Setup

Known target details from this project:

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
- AES key: optional/blank for this firmware unless WebRTC auth later proves otherwise

The important thing: `GO2_IP` should point at the dog, not the Jetson. For this setup that means:

```env
GO2_IP=192.168.123.121
```

## What This App Does

The app is intentionally small:

1. Connect to the Go2 Air over local-network WebRTC.
2. Start a REPL prompt.
3. Send each typed prompt to Ollama with a strict tool schema.
4. Execute exactly one robot tool call.
5. Enforce safety in Python regardless of what the model returns.

The available tools are:

- `robot_stand_up`
- `robot_sit_down`
- `robot_stop`
- `robot_move(vx, vy=0, vyaw=0, duration_s=0.35)`

The robot driver enforces conservative velocity caps, finite-number validation, a hard duration cap, a deadman loop, and stop-on-error behavior. The model is treated as a command chooser, not a trusted safety system.

## Safety Defaults

The limits live in `src/go2_local_brain/safety/limits.py`:

```python
MAX_VX = 0.35
MAX_VY = 0.20
MAX_VYAW = 0.45
DEFAULT_MOVE_DURATION_S = 0.35
MAX_MOVE_DURATION_S = 1.0
DEADMAN_TIMEOUT_S = 0.75
```

These are deliberately small. Do not raise them until the robot has been tested in a clear area with a human ready to intervene.

Important behavior:

- `robot_stand_up` sends `StandUp`, waits briefly, then sends `BalanceStand`.
- `robot_move` publishes `Move` at about 20 Hz, then sends `StopMove` at the end.
- If the LLM returns no tool call, an unknown tool, bad arguments, NaN, infinity, or any failed tool call, the brain calls `stop()`.
- If no fresh command arrives within the deadman timeout, the driver sends zero velocity.
- The app subscribes to passive sport-state telemetry, but does not yet block movement based on telemetry because the exact firmware schema still needs real hardware confirmation.

## Repository

Private GitHub repo:

```text
https://github.com/creeskis/go2_local_brain
```

Keep it private. The code does not contain a secret key, but the docs and config describe real robot network details, firmware version, a jailbroken robot, and deployment assumptions. That information should not be public by default.

## Clone Onto A WSL Instance

Use this path when testing from a WSL instance before moving to the Jetson.

```bash
cd ~
mkdir -p robotics
cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
```

If HTTPS asks for credentials, use your GitHub username and a GitHub personal access token. GitHub no longer accepts normal account passwords for git HTTPS pushes/clones in many flows.

## WSL Instance Network Notes

This app can run from a WSL instance if the WSL instance can reach the Go2 Air subnet.

Recommended `.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
memory=24GB
processors=8
```

With mirrored networking, the WSL instance usually shares the Windows host network interfaces directly, which makes the `192.168.123.x` robot subnet reachable.

Check reachability from WSL:

```bash
ping 192.168.123.121
ping 192.168.123.18
```

If the robot cannot be reached from WSL:

- Confirm mirrored networking is enabled.
- Restart WSL after changing `.wslconfig`:

```powershell
wsl --shutdown
```

Then reopen the WSL instance.

The project assumes outbound WebRTC traffic is allowed. DimensionOS already working through the same WSL network path is a good sign that WebRTC routing is basically correct.

## Install On A WSL Instance

From the cloned repo:

```bash
cd ~/robotics/go2_local_brain
bash bootstrap.sh
```

That script installs apt prerequisites, creates `.venv`, installs the package in editable mode, creates `.env` from `.env.example`, and runs the import smoke test.

Manual install:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git portaudio19-dev

cd ~/robotics/go2_local_brain
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
cp .env.example .env
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
```

## Configure `.env`

The normal target config is:

```env
GO2_IP=192.168.123.121
GO2_AES_128_KEY=
OLLAMA_MODEL=qwen3:1.7b
# OLLAMA_HOST=
# FORCE_MOTION_MODE=
```

Meaning:

- `GO2_IP`: the dog control endpoint. For this Go2 Air in STA mode, use `192.168.123.121`.
- `GO2_AES_128_KEY`: leave blank for firmware `1.1.7` unless WebRTC auth fails and a key is retrieved later.
- `OLLAMA_MODEL`: use `qwen3:1.7b` for Jetson Orin Nano compatibility.
- `OLLAMA_HOST`: only set when the Python app and Ollama server are in different environments.
- `FORCE_MOTION_MODE`: leave blank unless commands connect but appear to be ignored.

## Ollama Host Rules

`OLLAMA_HOST` tells the Python Ollama client where the Ollama server is.

Leave `OLLAMA_HOST` unset when:

- The app runs on the Jetson and Ollama also runs on the Jetson.
- The app runs in a WSL instance and Ollama also runs in that same WSL instance.

Set `OLLAMA_HOST` when the app and Ollama are separated:

```bash
export OLLAMA_HOST=http://<ollama-machine-ip>:11434
```

Examples:

```bash
# App in WSL, Ollama on the Windows host:
export OLLAMA_HOST=http://<windows-host-ip>:11434

# App on Jetson, Ollama on another machine:
export OLLAMA_HOST=http://<other-machine-ip>:11434
```

For the final Jetson setup, leave it unset.

## Ollama Model Choice

Default:

```env
OLLAMA_MODEL=qwen3:1.7b
```

Why `qwen3:1.7b`:

- It should run acceptably on Jetson Orin Nano.
- The brain only needs one simple bounded tool call per prompt.
- Safety is enforced by the driver, not the model.

Bigger optional models for an offboard planner:

- `qwen3:8b`: DimensionOS uses this for its Go2 Ollama agentic blueprint.
- `lfm2.5:8b`: stronger local tool-calling candidate, but heavier.
- `gpt-oss:20b`: larger planning model, probably too heavy for live Jetson use.

A good architecture is small model on the Jetson for live commands, with larger models only as optional offboard planners that emit bounded skills.

Pull the default model:

```bash
ollama pull qwen3:1.7b
```

Check Ollama:

```bash
ollama list
curl http://localhost:11434/api/tags
```

## Jetson Orin Nano Setup

JetPack `6.2.1` is Ubuntu 22.04 based and commonly ships Python 3.10. This project supports Python `>=3.10`, so the default JetPack Python should work.

On the Jetson:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git portaudio19-dev

cd ~
mkdir -p robotics
cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
bash bootstrap.sh
```

Install and prepare Ollama on the Jetson, then:

```bash
ollama pull qwen3:1.7b
ollama list
```

Configure `.env`:

```bash
cp .env.example .env
nano .env
```

Expected Jetson `.env`:

```env
GO2_IP=192.168.123.121
GO2_AES_128_KEY=
OLLAMA_MODEL=qwen3:1.7b
# OLLAMA_HOST=
# FORCE_MOTION_MODE=
```

Then run:

```bash
source .venv/bin/activate
python -m unittest discover -s tests
python -m go2_local_brain.main
```

## First Hardware Bring-Up Order

Do not start with natural language movement. Bring it up in this order.

1. Confirm network:

```bash
ping 192.168.123.121
```

2. Confirm Ollama:

```bash
ollama list
curl http://localhost:11434/api/tags
```

3. Confirm imports/tests:

```bash
source .venv/bin/activate
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
```

4. Run the app:

```bash
python -m go2_local_brain.main
```

5. Try only stationary commands first:

```text
stop
stand up
sit down
```

6. Then try tiny movement commands in a clear area:

```text
move forward a tiny bit
turn left slowly
stop
```

Watch logs for:

- `Go2 WebRTC connected at 192.168.123.121`
- `sport state: mode=... gait=...`
- `move clamped: ...`
- `motion mode already 'normal'`
- `switching motion mode: ... -> 'normal'`

## Motion Mode Notes

Firmware `1.1.7` matters because upstream `unitree_webrtc_connect` has both normal sport examples and MCF sport examples for firmware `>=1.1.7`.

The commands this app uses have matching IDs in both `SPORT_CMD` and `SPORT_CMD_MCF`:

```text
BalanceStand = 1002
StopMove     = 1003
StandUp      = 1004
StandDown    = 1005
Move         = 1008
Sit          = 1009
```

So the app does not need a command-table switch for the current tool surface.

The upstream MCF example warns that after `StandUp`, joints may be locked until `BalanceStand` is called. This app therefore chains:

```text
StandUp -> wait 2.5s -> BalanceStand
```

If the robot connects but ignores movement, try:

```env
FORCE_MOTION_MODE=normal
```

Then rerun the app. The driver will query `MOTION_SWITCHER`, switch to `normal` if needed, and wait about 5 seconds before accepting commands.

## WebRTC And Firmware Notes

This app uses `unitree_webrtc_connect` v2.x. There is no `SportClient` class in this version. The driver talks to:

```python
conn.datachannel.pub_sub
```

and publishes sport requests to:

```python
RTC_TOPIC["SPORT_MOD"]
```

The confirmed `Move` payload shape is:

```json
{"x": 0.2, "y": 0.0, "z": 0.0}
```

Where:

- `x` is forward velocity in m/s.
- `y` is lateral velocity in m/s, positive left.
- `z` is yaw rate in rad/s, positive counter-clockwise.

Firmware `1.1.7` is below the upstream `1.1.15+` AES-key threshold. That matches the current assumption that `GO2_AES_128_KEY` can remain blank. If WebRTC auth fails later, retrieve the key and set it in `.env`.

## Running The App

From the project root:

```bash
source .venv/bin/activate
python -m go2_local_brain.main
```

The REPL starts:

```text
Go2 local brain ready. Type a command, or 'quit' to exit.
go2>
```

Example prompts:

```text
stand up
move forward a tiny bit
turn left slowly
stop
sit down
quit
```

The LLM prompt is intentionally compact and example-driven because `qwen3:1.7b` is small. Each user prompt should map to exactly one tool call.

## Running Tests

Run all non-hardware tests:

```bash
source .venv/bin/activate
python -m unittest discover -s tests
```

The tests cover:

- Ollama tool-call extraction from dict/object/JSON-string shapes.
- Stop behavior on missing/unknown tools.
- NaN/inf rejection.
- Duration capping.
- Move envelope shape.
- Closed-channel failures.
- Sport-state telemetry parsing.
- Motion-mode response parsing.

A clean test run should end with `OK`.

## Upgrade The Code On A WSL Instance

From the WSL instance:

```bash
cd ~/robotics/go2_local_brain
git status
git pull
source .venv/bin/activate
pip install -e .
python -m unittest discover -s tests
```

If dependencies changed:

```bash
pip install --upgrade pip
pip install -e .
```

If `.env.example` changed, compare it with your local `.env`:

```bash
diff -u .env.example .env
```

Do not overwrite `.env` blindly; it contains local deployment settings.

## Upgrade The Code On Jetson

On the Jetson:

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

If the app no longer starts after an upgrade, check:

```bash
python3 --version
pip show unitree_webrtc_connect ollama python-dotenv rerun-sdk
ollama list
cat .env
```

## What To Change Safely

Safe-ish changes:

- Tweak the LLM examples in `src/go2_local_brain/brain/local_llm.py`.
- Add more tests under `tests/`.
- Add more logging.
- Add passive telemetry fields from sport state.
- Change `OLLAMA_MODEL` in `.env`.
- Set `FORCE_MOTION_MODE=normal` when diagnosing ignored commands.

Changes that need hardware caution:

- Raising `MAX_VX`, `MAX_VY`, or `MAX_VYAW`.
- Raising `MAX_MOVE_DURATION_S`.
- Raising `DEADMAN_TIMEOUT_S`.
- Automatically switching motion modes on every startup.
- Gating movement on sport telemetry before confirming the exact schema.
- Adding advanced MCF actions such as flips, jumps, or handstand-style commands.

## Code Map

```text
src/go2_local_brain/
  main.py                    Entry point and REPL wiring
  config.py                  Environment/.env loader
  brain/local_llm.py          Ollama tool-calling brain
  driver/webrtc_client.py     Unitree WebRTC driver wrapper
  safety/limits.py            Motion caps and deadman timeout
  viz/rerun_logger.py         Placeholder Rerun logger
scripts/
  smoke_test_imports.py       Import/package smoke test
tests/
  test_brain.py               Brain/unit tests
  test_driver.py              Driver/unit tests
```

## Key Environment Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `GO2_IP` | `192.168.123.121` | Dog WebRTC/control endpoint |
| `GO2_AES_128_KEY` | blank | Optional AES key for newer firmware/auth cases |
| `OLLAMA_MODEL` | `qwen3:1.7b` | Ollama model name |
| `OLLAMA_HOST` | unset | Only set when Ollama is remote from the app |
| `FORCE_MOTION_MODE` | unset | Optional mode switch, usually `normal` |

## Troubleshooting

### Robot does not connect

Check:

```bash
ping 192.168.123.121
```

Then verify `.env`:

```bash
cat .env
```

Common causes:

- `GO2_IP` accidentally points to the Jetson (`192.168.123.18`) instead of the dog.
- The WSL instance cannot route to the `192.168.123.x` subnet.
- Another client is already connected to the dog over WebRTC.
- Firmware unexpectedly requires the AES key.

### App connects but movement is ignored

Try:

```env
FORCE_MOTION_MODE=normal
```

Then rerun. Also watch for `sport state: mode=... gait=...` logs.

### Ollama fails

Check:

```bash
ollama list
curl http://localhost:11434/api/tags
```

If Ollama is remote, set `OLLAMA_HOST`.

### Model returns no tool call

`qwen3:1.7b` is small. Keep prompts simple:

```text
stand up
move forward a tiny bit
turn left slowly
stop
sit down
```

If testing on a stronger machine, try:

```env
OLLAMA_MODEL=qwen3:8b
```

or:

```env
OLLAMA_MODEL=lfm2.5:8b
```

### pyaudio/portaudio build fails

Install:

```bash
sudo apt install -y portaudio19-dev
```

Then reinstall:

```bash
source .venv/bin/activate
pip install -e .
```

### WSL instance cannot see the robot subnet

Use mirrored networking:

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

Then reopen the WSL instance and retry:

```bash
ping 192.168.123.121
```

## Future Work

Good next improvements:

- Add a hardcoded diagnostic command path that bypasses Ollama for `stand`, `balance`, `tiny move`, and `stop`.
- Add a `robot_balance_stand` tool.
- Add a `robot_recovery_stand` tool.
- Add a higher-level `relative_move(forward_m, left_m, yaw_deg)` skill.
- Add telemetry-driven movement gates after confirming sport-state schema on real hardware.
- Add a DimensionOS-compatible blueprint wrapper.
- Add structured logs for first-run bring-up.
- Add optional upstream example checks for `sportmode.py`, `sportmode_mcf.py`, and `sportmodestate.py`.

## Golden Path Summary

For a WSL instance:

```bash
cd ~
mkdir -p robotics
cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
bash bootstrap.sh
ollama pull qwen3:1.7b
source .venv/bin/activate
python -m unittest discover -s tests
python -m go2_local_brain.main
```

For Jetson:

```bash
cd ~
mkdir -p robotics
cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain
bash bootstrap.sh
ollama pull qwen3:1.7b
source .venv/bin/activate
python -m unittest discover -s tests
python -m go2_local_brain.main
```

Target `.env`:

```env
GO2_IP=192.168.123.121
GO2_AES_128_KEY=
OLLAMA_MODEL=qwen3:1.7b
# OLLAMA_HOST=
# FORCE_MOTION_MODE=
```

Only set `FORCE_MOTION_MODE=normal` if the app connects but sport commands appear to be ignored.
