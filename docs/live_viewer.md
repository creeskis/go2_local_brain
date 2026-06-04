# Live Video And LiDAR Viewer

Run this in a separate terminal from the LLM control REPL. The viewer opens its own WebRTC connection to the Go2 and serves a browser page from the WSL instance.

```bash
cd ~/robotics/go2_local_brain
git pull
source .venv/bin/activate
pip install -e .
python -m go2_local_brain.viewer --host 0.0.0.0 --port 8765
```

Open this from the host browser:

```text
http://localhost:8765
```

If `localhost` does not route from the host into the WSL instance, use the WSL instance IP:

```bash
hostname -I
```

Then open:

```text
http://<wsl-ip>:8765
```

The page has two panes:

- Left: live robot video as an MJPEG stream.
- Right: decoded LiDAR point cloud from `rt/utlidar/voxel_map_compressed` rendered with Three.js.

The viewer follows upstream `unitree_webrtc_connect` examples:

- Video: `conn.video.switchVideoChannel(True)` and `conn.video.add_track_callback(...)`.
- LiDAR: `rt/utlidar/switch` set to `on`, subscribe to `rt/utlidar/voxel_map_compressed`, read decoded `message["data"]["data"]["positions"]` as flat xyz triples.

Useful variants:

```bash
python -m go2_local_brain.viewer --host 0.0.0.0 --port 8765 --no-video
python -m go2_local_brain.viewer --host 0.0.0.0 --port 8765 --no-lidar
```

Stop the viewer with `Ctrl+C`. It attempts to switch LiDAR off during shutdown.
