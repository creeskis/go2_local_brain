# go2_local_brain

Host-computer control stack for the Unitree Go2 Air.

Run this on the work computer in WSL Kali Linux. Do not run the cockpit stack
on the Jetson. The Jetson is not part of the normal control path anymore.

## Update On WSL

```bash
cd ~/robotics/go2_local_brain
git pull
source .venv/bin/activate
pip install -e .
```

Optional Face ID backend on CPU:

```bash
pip install opencv-python-headless
# optional identity enrollment; heavier:
pip install face_recognition
```

If Face ID is too heavy on the work computer, leave the backend uninstalled.
The local cockpit will still draw detected face boxes, but identity enrollment
needs a working embedding backend.

## Minimal `.env`

```env
GO2_IP=192.168.123.121
GO2_WEBRTC_METHOD=LocalSTA
GO2_AES_128_KEY=
FORCE_MOTION_MODE=normal
GO2_FACE_BACKEND=face_recognition
GO2_FACE_ENABLED=1
GO2_FACE_INTERVAL_S=1.25
GO2_FACE_DETECT_MAX_WIDTH=360
GO2_JPEG_QUALITY=68
OLLAMA_MODEL=qwen2.5:0.5b
```

If `192.168.123.121` does not work for video/control, try the dog Ethernet IP
that is known to work on your setup:

```env
GO2_IP=192.168.123.161
```

Keep private passwords out of git. Leave `GUN_*` values blank unless you are
actively testing the optional host-side USB trigger path.

## Main Cockpits

Run one WebRTC cockpit/viewer at a time unless you are deliberately testing
multiple clients. The dog can get cranky when several viewers compete for the
same video/data connection.

For presentation video, use these in this order:

1. `run_local_cockpit.sh` for the clean operator view.
2. `run_wasd_lidar.sh` when the demo needs driving and LiDAR in one page.
3. `run_ai_demo.sh` only for the AI segment; it starts Ollama for the session
   if Ollama is not already running, then shuts down only the Ollama process it
   started when you exit.

### Local Operator Cockpit

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
./scripts/run_local_cockpit.sh
```

Open:

```text
http://127.0.0.1:8775
```

Use this for:

- Live video
- WASD driving
- Bluetooth/Xbox-style controller driving
- Face ID boxes and enrollment
- Motion buttons
- Optional host-side trigger controls

### Home Simulation Cockpit

Use this away from the work computer when the dog and Jetson are not
available. It runs the same browser controls against a fake robot, simulated
gun relay, and either the host webcam or generated video.

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
./scripts/run_sim_cockpit.sh
```

Open:

```text
http://127.0.0.1:8785
```

Useful switches:

```bash
GO2_SIM_CAMERA=-1 ./scripts/run_sim_cockpit.sh   # generated video only
GO2_SIM_CAMERA=0 ./scripts/run_sim_cockpit.sh    # host webcam
```

### WASD + LiDAR Demo

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
./scripts/run_wasd_lidar.sh
```

Open:

```text
http://127.0.0.1:8774
```

Use this when you need manual driving and LiDAR on the same screen for a demo.
It is separate from the lean operator cockpit.

### LiDAR Viewer

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
./scripts/run_lidar_viewer.sh
```

Open:

```text
http://127.0.0.1:8765
```

Use this only for LiDAR. It is intentionally separate from the operator
cockpit.

### AI Demo

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
./scripts/run_ai_demo.sh
```

Open:

```text
http://127.0.0.1:8778
```

Use this for the AI demo segment. The script checks `OLLAMA_HOST`, starts
`ollama serve` only if needed, and stops only the Ollama process it started
when the script exits.

Tiny model check:

```bash
OLLAMA_MODEL=qwen2.5:0.5b GO2_AI_AUTO_PULL=1 ./scripts/run_ai_demo.sh
python scripts/eval_model_tools.py
```

If `0.5b` is too weak for tool calls, try `qwen2.5:1.5b` before moving to
larger models.

### Webcam Face ID Test

Use this on the host computer when the dog is not available:

```bash
pip install opencv-python
python scripts/webcam_faceid_test.py --seconds 8
```

That tests lightweight face boxes only. Identity enrollment needs the heavier
backend:

```bash
pip install face_recognition
python scripts/webcam_faceid_test.py --backend face_recognition --label Cooper --seconds 8
```

### Old AI / Autonomy Cockpit

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
./scripts/run_ai_cockpit.sh
```

Open:

```text
http://127.0.0.1:8777
```

Use this for the old AI/autonomy/map cockpit. Keep it separate from the lean
local operator cockpit.

## Quick Checks

Confirm WSL can reach the dog:

```bash
ping -c 3 192.168.123.121
```

Reset stale dog STA/runtime state before making a fresh STA attempt:

```bash
./scripts/reset_dog_sta_runtime_over_ssh.sh
```

If eth0 still keeps the `192.168.123.0/24` route and steals LocalSTA traffic:

```bash
DOG_REMOVE_PRIMARY_ETH_STA=1 ./scripts/reset_dog_sta_runtime_over_ssh.sh
```

Check imports:

```bash
python scripts/smoke_test_imports.py
```

Run non-hardware tests:

```bash
PYTHONPATH=src python -m unittest tests.test_driver tests.test_control_modes tests.test_viewer tests.test_face_id
```

Check shell scripts:

```bash
bash -n scripts/*.sh
```

## Ports

| Script | URL | Purpose |
| --- | --- | --- |
| `./scripts/run_local_cockpit.sh` | `http://127.0.0.1:8775` | Main operator cockpit |
| `./scripts/run_sim_cockpit.sh` | `http://127.0.0.1:8785` | Home simulation cockpit |
| `./scripts/run_wasd_lidar.sh` | `http://127.0.0.1:8774` | WASD + LiDAR demo |
| `./scripts/run_lidar_viewer.sh` | `http://127.0.0.1:8765` | Separate LiDAR viewer |
| `./scripts/run_ai_demo.sh` | `http://127.0.0.1:8778` | AI + WASD + LiDAR demo |
| `./scripts/run_ai_cockpit.sh` | `http://127.0.0.1:8777` | Old AI/autonomy cockpit |

## Kept For Later

Jetson and relay scripts are kept for later hardware work, but they are not
part of the normal presentation/demo flow. Do not run the cockpit stack on the
Jetson.

## Notes

- Everything normal runs on the host computer in WSL.
- Do not move the main cockpit back to the Jetson.
- Keep LiDAR separate.
- Keep the old AI cockpit separate.
- Keep secrets out of the repo.
