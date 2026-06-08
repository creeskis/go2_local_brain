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

For human boxes, optional face boxes, and follow mode:

```bash
python -m go2_local_brain.ai_autonomy_gui --host 0.0.0.0 --port 8775 --maps-dir maps --detector yolo --face-detection
```

For local-machine sound cues:

```bash
pip install -e ".[vision,audio]"
python -m go2_local_brain.ai_autonomy_gui --host 0.0.0.0 --port 8775 --maps-dir maps --detector yolo --face-detection --follow-source visual-or-sound
```

Sound following is intentionally conservative. A normal mono laptop microphone can tell the app that a loud sound happened, but it cannot reliably tell the direction of that sound. In that mode the robot scans for a person after a sound cue; it does not blindly walk toward sound.

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
- Human/face detection overlay when a detector is enabled.
- Follow Human start/step/stop controls.
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

The browser saves maps as JSON under `maps/` by default. Incomplete maps are kept as drafts so you can build them over several test sessions. A map is patrol-ready only when it has at least one waypoint and a non-empty patrol route whose names all exist in the waypoint list. Saving a patrol-ready map loads it automatically; saving an incomplete map does not activate or replace the current patrol map.

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

Camera-only reports whether a camera frame exists and returns no detections. It is not considered detector-ready. YOLO uses the optional `ultralytics` package and is considered ready only when a camera frame exists and the model can load. When `--face-detection` is enabled, the YOLO provider also tries OpenCV Haar face detection if `cv2` is installed.

The next detector can plug into the same interface by returning:

```python
Observation(
    timestamp=...,
    frame_available=True,
    detections=[
        Detection("person", 0.82, x=320, y=240, width=160, height=240)
    ],
    frame_width=640,
    frame_height=480,
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

## Follow Behavior

Follow mode lives in:

```text
src/go2_local_brain/autonomy/follow.py
```

It is not an LLM loop. It picks the highest-confidence `person` detection, then sends short movement windows:

- Person left/right of center: turn toward the box.
- Person far away: move forward slowly.
- Person too close: back away slightly.
- No person: scan in place.
- Sound cue without a person: scan in place and let visual detection reacquire the target.

Starting follow mode pauses the patrol supervisor so two autonomy loops do not fight over movement.

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

1. Add pose-topic integration from Claude's `claude/ai-mode-prep` branch after hardware probing.
2. Add a planner class that can ask Ollama for high-level choices from compact observations.
3. Add visual landmark localization so waypoints become less approximate.
4. Add no-go-zone enforcement.
5. Add patrol/follow reports: what was seen, where, and when.
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
