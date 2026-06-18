# Current Cockpits

Use these as separate surfaces. Do not run more than one WebRTC surface at the
same time unless you are deliberately testing whether the dog accepts multiple
clients.

## Local operator cockpit

```bash
./scripts/run_local_cockpit.sh
```

Default URL: `http://127.0.0.1:8775`

Purpose: video, WASD, Bluetooth/Xbox-style controller input, Face ID, motion
buttons, and the optional USB trigger relay. LiDAR is intentionally not part of
this cockpit.

## LiDAR viewer

```bash
./scripts/run_lidar_viewer.sh
```

Default URL: `http://127.0.0.1:8765`

Purpose: LiDAR-only view. This uses the standalone viewer path and keeps LiDAR
separate from the operator cockpit.

## WASD + LiDAR demo

```bash
./scripts/run_wasd_lidar.sh
```

Default URL: `http://127.0.0.1:8774`

Purpose: manual driving and LiDAR on one screen for demo recording.

## AI demo

```bash
./scripts/run_ai_demo.sh
```

Default URL: `http://127.0.0.1:8778`

Purpose: AI + WASD + LiDAR demo. The script starts `ollama serve` only if
needed and stops only the Ollama process it started when it exits.

## Old AI/autonomy cockpit

```bash
./scripts/run_ai_cockpit.sh
```

Default URL: `http://127.0.0.1:8777`

Purpose: the older AI/autonomy/map cockpit backed by
`go2_local_brain.ai_autonomy_gui`. It is separate from the lean local cockpit.

## Work-computer defaults

For the weaker Kali WSL host, keep Face ID light unless you know the machine can
handle more:

```env
GO2_FACE_ENABLED=1
GO2_FACE_INTERVAL_S=1.25
GO2_FACE_DETECT_MAX_WIDTH=360
GO2_JPEG_QUALITY=68
```

If the USB trigger relay is not in use, leave the `GUN_*` passwords blank.
