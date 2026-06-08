# AI-Only Autonomy Mode

AI-only mode is not meant to let the LLM directly stream velocity commands. It is a supervised autonomy loop:

```text
map route -> perception snapshot -> patrol supervisor -> short movement primitive -> stop/check -> next step
```

The LLM/planner layer should stay high level. Deterministic Python owns movement windows, state transitions, pause/stop, and the patrol route.

## Run

```bash
cd ~/robotics/go2_local_brain
source .venv/bin/activate
pip install -e .
python -m go2_local_brain.ai_autonomy_gui --host 0.0.0.0 --port 8775 --maps-dir maps
```

For YOLO image detection:

```bash
pip install -e ".[vision]"
python -m go2_local_brain.ai_autonomy_gui --host 0.0.0.0 --port 8775 --maps-dir maps --detector yolo --yolo-model yolov8n.pt
```

For a camera-only dry run that bypasses detector readiness:

```bash
python -m go2_local_brain.ai_autonomy_gui --host 0.0.0.0 --port 8775 --maps-dir maps --allow-no-detector
```

Open:

```text
http://localhost:8775
```

## What The GUI Does

The autonomy GUI shows:

- Live video.
- Map builder/saver/loader.
- Autonomy state.
- Current waypoint.
- Last observation summary.
- Last action.
- Event log.
- Buttons: save/load map, check image detection, activate, pause, resume, step once, stop.

It does not expose WASD. This mode is meant for watching the autonomy supervisor make decisions.

## Current State Machine

```text
idle
arming
patrolling
scanning
investigating
paused
error_stop
```

`Activate AI Mode` starts the supervisor task. `Pause` stops movement and holds state. `Resume` returns to patrol. `STOP` cancels the task and sends stop to the robot.

## Map Format

There is intentionally no configured `home.json` in the repo. Create your own map in the browser, save it, then load it before activating autonomy.

Shape:

```json
{
  "name": "home-starter",
  "waypoints": {
    "home": {"x": 0.0, "y": 0.0, "yaw": 0.0, "note": "Starting point"},
    "room_center": {"x": 1.2, "y": 0.0, "yaw": 0.0}
  },
  "patrol_route": ["home", "room_center"],
  "no_go_zones": ["stairs"]
}
```

This is a rough relative map, not real SLAM. Coordinates are first-pass patrol hints. Keep distances small until localization is added.

The browser saves maps as JSON under `maps/` by default. A map is patrol-ready only when it has at least one waypoint and a non-empty patrol route whose names all exist in the waypoint list.

## Perception Interface

Perception lives in:

```text
src/go2_local_brain/autonomy/perception.py
```

Current providers:

```text
CameraOnlyPerceptionProvider
YoloPerceptionProvider
```

Camera-only reports whether a camera frame exists and returns no detections. It is not considered detector-ready. YOLO uses the optional `ultralytics` package and is considered ready only when a camera frame exists and the model can load.

The next detector can plug into the same interface by returning:

```python
Observation(
    timestamp=...,
    frame_available=True,
    detections=[
        Detection("person", 0.82, x=0.4, y=0.3, width=0.2, height=0.5)
    ],
)
```

Good detector backends for later:

- YOLO nano model for object detection on Jetson.
- AprilTags for known-location visual landmarks.
- A simple doorway/marker detector for map localization.

## Patrol Behavior

The supervisor calls:

```python
AutonomySupervisor.step_once()
```

Each step:

1. Captures a compact observation from the perception provider.
2. If an interesting detection is present, scans/investigates.
3. Otherwise selects the next map waypoint.
4. Calls `AutonomyNavigator.move_toward()`.
5. Logs the action.

The navigator only sends short movement windows:

- Small turn if the waypoint is off-angle.
- Short forward step if aligned.
- Small scan if close to a waypoint.

## Why This Is Safer Than Raw LLM Control

The LLM should eventually choose high-level actions like:

```text
continue_patrol
go_to_waypoint room_center
inspect_detection person
pause_and_report
return_home
```

It should not own raw continuous velocity. The Python supervisor can enforce:

- Short move windows.
- Stop between autonomy decisions.
- Pause/resume.
- Route boundaries.
- No-go zone awareness later.
- Error-stop state.

## Next Implementation Targets

1. Add a YOLO/AprilTag provider behind `PerceptionProvider`.
2. Add a planner class that can ask Ollama for high-level choices from compact observations.
3. Add visual landmark localization so waypoints become less approximate.
4. Add no-go-zone enforcement.
5. Add patrol reports: what was seen, where, and when.
6. Add saved patrol logs under a local `runs/` or `logs/` directory.

## Test Without Hardware

```bash
python -m compileall -q src
PYTHONPATH=src python -m unittest tests.test_autonomy
```

Full local checks:

```bash
python scripts/smoke_test_imports.py
python -m unittest discover -s tests
```
