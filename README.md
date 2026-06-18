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
GO2_FACE_INTERVAL_S=0.65
GO2_FACE_DETECT_MAX_WIDTH=640
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
| `./scripts/run_lidar_viewer.sh` | `http://127.0.0.1:8765` | Separate LiDAR viewer |
| `./scripts/run_ai_cockpit.sh` | `http://127.0.0.1:8777` | Old AI/autonomy cockpit |

## Notes

- Everything normal runs on the host computer in WSL.
- Do not move the main cockpit back to the Jetson.
- Keep LiDAR separate.
- Keep the old AI cockpit separate.
- Keep secrets out of the repo.
