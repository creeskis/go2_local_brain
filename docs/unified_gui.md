# Unified GUI

The unified GUI runs manual control, AI commands, live video, and LiDAR from one Python process and one WebRTC connection.

Run it from a WSL instance:

```bash
cd ~/robotics/go2_local_brain
git pull
source .venv/bin/activate
pip install -e .
python -m go2_local_brain.gui --host 0.0.0.0 --port 8765
```

Open this from the host browser:

```text
http://localhost:8765
```

If `localhost` does not route into the WSL instance:

```bash
hostname -I
```

Then open:

```text
http://<wsl-ip>:8765
```

## What It Includes

- Manual drive buttons.
- Keyboard drive: `W`, `A`, `S`, `D`, `Q`, `E`.
- Spacebar stop.
- Posture/action buttons: stand, sit, recovery, greet, dance, jump, pounce, handstand, backstand.
- AI command text box using the configured Ollama model.
- Live robot video.
- Live LiDAR point cloud.

## Important Notes

Use the unified GUI instead of running `python -m go2_local_brain.main` and `python -m go2_local_brain.viewer` at the same time. The GUI intentionally shares one WebRTC connection across controls, AI, video, and LiDAR.

The manual hold buttons send short repeated move commands while held and send `stop` when released. The spacebar always sends stop.

The AI command box calls the same `LocalRobotBrain` tool path as the REPL, so prompts like these still work:

```text
turn right 90 degrees, then walk forward
stand on your hind legs
make up a dance
explore in relaxed mode for 10 seconds
```

## LiDAR Troubleshooting

The LiDAR HUD shows:

```text
raw=<callbacks> rendered=<clouds> parseErrors=<bad-shape> dropped=<throttled>
```

- `raw=0`: the robot is not publishing on either LiDAR topic, or LiDAR did not switch on.
- `raw>0` and `parseErrors>0`: the decoded message shape is different than expected; check the latest GUI log line for `LiDAR parse shape`.
- `raw>0` and `rendered>0`: the backend is receiving point clouds; if the pane is visually blank, use the mouse wheel / drag in the LiDAR panel to reframe the Three.js camera.

The GUI subscribes to both `rt/utlidar/voxel_map` and `rt/utlidar/voxel_map_compressed` and down-samples point clouds before sending them to the browser.
