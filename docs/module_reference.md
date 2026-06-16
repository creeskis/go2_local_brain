# Module reference — how every piece works and gets called

This is the engineering companion to the README. It documents what each
module does, its key public API, and **how it is actually invoked** in the
running system.

## Where this stack sits (from the forensic writeup)

The Go2 Air is a layered robotics platform, not a single Linux box taking
motor commands. Operator input → WebRTC/Unitree bridge → DDS/RTPS middleware
→ Unitree services (`sport_mode`, `mcf_main`, `robot_state_se`,
`unitree_lidar_server`, `obstacles_avoi`) → motion-control stack → hardware.
`mcf_main`/`sport_mode` run the active stabilization loop — kill them while
the dog is standing and it drops.

`go2_local_brain` is a **supervisory layer above that stack**. It never drives
motors directly. It connects to the dog's WebRTC bridge (STA mode,
`192.168.123.121`, signaling port **9991**), sends *bounded high-level*
requests (stand / move(vx,vy,vyaw) / sport actions), and reads back
video + LiDAR + sport-state telemetry. Everything below is built around that
single safe boundary.

```
operator (browser / CLI)
        │
        ▼
  cockpit / brain         ← this repo, on your computer
        │  bounded calls
        ▼
  Go2WebRTCClient (driver) ← clamps + dead-man stop
        │  WebRTC :9991
        ▼
  Unitree bridge → DDS → sport_mode/mcf_main → motors
```

Data flow at runtime: the **driver** owns the WebRTC connection and pushes
sport-state + LiDAR + video to in-memory caches; **perception** turns frames
into detections/identities; **mapping/navigation** turns pose + LiDAR into
waypoint motion; the **cockpit** is the aiohttp server wiring it all to a
browser; the **brain** is an optional LLM planner that can only call the same
bounded tools.

---

## Foundation

### `config.py` — `load_config() -> AppConfig`
Reads `.env` (via python-dotenv) + process env into a frozen `AppConfig`
(`go2_ip`, `go2_webrtc_method`, AES key, remote creds, `ollama_model`,
`force_motion_mode`, exploration settings). **Called once** at the top of
every entry point (`ai_autonomy_gui.run`, `viewer`, `face_viewer`,
`main`, `enroll_face.py`) before constructing the driver. Single source of
truth for "which dog, how do we reach it."

### `safety/limits.py` — `clamp(value, lo, hi)` + constants
`MAX_VX/MAX_VY/MAX_VYAW`, `DEFAULT_MOVE_DURATION_S`, dead-man timeout. **Called
by** the driver's `move()` and the control resolver. This is the numeric
backstop the writeup's safety model depends on; nothing above it can exceed
these even if the LLM or a buggy client asks.

---

## Driver — the one path to the robot

### `driver/webrtc_client.py`
`Go2Config` (connection params) + `Go2WebRTCClient` (the wrapper). This is the
**only** module that talks WebRTC; everything else calls it.

Lifecycle:
- `await connect()` — opens the WebRTC peer to `:9991`, locates the data
  channel pub/sub, caches `SPORT_CMD` + `SPORT_CMD_MCF`, subscribes to
  sport-state/LiDAR, starts the dead-man loop.
- `await close()` — tears the connection down cleanly.

Motion (all clamped + dead-man-guarded):
- `await move(vx, vy, vyaw, duration_s)` — the core velocity command (maps to
  Unitree `Move` 1008, valid in BalanceStand).
- `await stop()` — explicit halt.
- `await stand_up()` / `sit_down()` / `balance_stand()` / `recovery_stand()`.
- `await advanced_action(name)` — named one-shots resolved against
  `SPORT_CMD`/`SPORT_CMD_MCF` (MCF first): greet, dance, jump, pounce,
  stretch, wiggle, handstand, backstand, **front/back/left/right_flip**.
- `await sport_command(name, parameter)` / `sport_command_response(...)` —
  exact SDK command by name (for advanced/diagnostic use).
- `await turn_180(direction)` / `turn_degrees(direction, degrees)`.
- `await dance_move(style)` / `sequence(steps)` — composite macros.
- `await explore_room(duration_s, mode)` — short autonomous roam.
- `await set_motion_mode(target)` / `motion_mode_status()` — switch/inspect
  the firmware motion mode (the `FORCE_MOTION_MODE=normal` lever).
- `available_sport_commands()` / `telemetry_report()` — introspection.

**Called by:** the cockpit's manual + control endpoints, the navigator, the
supervisor, the brain's tools, and `enroll_face.py` (just for the video track).

---

## Perception

### `autonomy/perception.py`
`Detection` (label + box), `Observation` (frame + detections + dims), and the
`PerceptionProvider` interface with three impls:
- `CameraOnlyPerceptionProvider` — reports frame availability, no detection.
- `CallbackPerceptionProvider` — wraps an external producer.
- `YoloPerceptionProvider(frame_supplier, ...)` — runs Ultralytics YOLO on the
  latest JPEG; optional Haar face boxes.

`await provider.observe() -> Observation` each tick. **Called by** the
cockpit's perception loop (updates `self._latest_observation`), the follow
controller, the supervisor, and targeting. `best_human_detection(obs)` and
`detection_to_dict(...)` are helpers for follow + the browser overlay.

### `autonomy/face_id.py` — identity
- `cosine_similarity(a, b)` — pure-Python vector match (testable, no ML).
- `FaceDatabase` — labeled embeddings persisted as JSON
  (`~/.config/go2_local_brain/faces.json`): `enroll`, `identify`, `remove`,
  `labels`, `save`/`load`/`load_or_empty`.
- `FaceEmbedder` interface + `NullFaceEmbedder` (default),
  `FaceRecognitionEmbedder` (dlib 128-d), `InsightFaceEmbedder` (ONNX 512-d);
  `build_face_embedder(backend)` factory (lazy ML imports).
- `FaceIdentifier(embedder, db)` — `identify_faces(image, boxes)` and
  `enroll_from_image(label, image, box)`.

**Called by:** `scripts/enroll_face.py` (enroll flow) and `face_viewer.py` /
the cockpit face panel (live identify). This is the "embedding backend +
database + match" box in the writeup's face diagram.

### `autonomy/face_tracker.py` — stability over time
`FaceTracker.update(faces) -> [FaceTrack]` assigns stable `track_id`s by
centroid association and smooths the label by majority vote across frames
(`confident_tracks()` returns the trusted ones). **Called by** the face
consumer each frame so a flickering single-frame label doesn't whipsaw the UI.

### `autonomy/follow.py` — person following
`HumanFollowController(mover).step(observation, sound_cue)` centers + paces a
detected person; `plan()` is the pure decision (testable). `SoundCue` +
`LocalSoundLevelProvider` allow a mic to trigger a re-scan. **Called by** the
cockpit's follow endpoints.

---

## LiDAR + pose

### `autonomy/lidar_map.py`
`points_from_lidar_payload(payload)` decodes the dog's compressed voxel map
into points; `LidarObstacleField.update(points)` bins them into front/left/
right/rear distances (`current_summary()`, `recommended_avoidance_turn()`);
`LidarTransform` calibrates orientation; `LidarLocalMapper.add_scan(pose,
points)` accumulates an occupancy view. This is the safety/perception use of
LiDAR the writeup describes (range data feeding `obstacles_avoi` on the dog —
here we consume the same stream client-side). **Called by** the cockpit LiDAR
handler and the navigator's obstacle guard.

### `autonomy/local_map.py`
`Pose2D` + `LocalMapState`. `update_from_sport_state(sport_state)` converts the
dog's reported pose into an **origin-locked** local frame so coordinates are
stable within a session; `lock_to_map_pose(map_pose, sport_state)` is the
relocalization hook that aligns a fresh boot to a saved map's anchor (the seed
for a persistent world frame); `age_s()`/`is_fresh()` flag stale pose during a
WebRTC drop. **Called by** the navigator (to know where the dog is) and the
cockpit map view.

---

## Mapping, navigation, autonomy

### `autonomy/map.py`
`Waypoint`, `PatrolMap` (`next_waypoint`, `validate_for_patrol`), and
`load_patrol_map` / `save_patrol_map` / `list_patrol_maps` /
`empty_patrol_map`. JSON maps under `maps/`. **Called by** the cockpit map
builder and the supervisor (route source).

### `autonomy/navigator.py`
`AutonomyNavigator(client, local_map, lidar_obstacles).move_toward(waypoint)`
returns a structured `NavStep` (`driving`/`pivot`/`avoid`/`align`/
`scan_complete`) — blended proportional control with an obstacle guard, the
rewrite the writeup mentions (no more stop-turn-walk stutter, correct yaw
sign, hold-until-arrival). **Called by** the supervisor each tick.

### `autonomy/supervisor.py`
`AutonomySupervisor(map, navigator, perception)` — the patrol state machine:
`activate()`, `pause()`, `resume()`, `stop()`, `status()`, and `step_once()`
(one decision, per-step timeout). Advances the route only on
`NavStep.kind == "scan_complete"`. **Called by** the cockpit autonomy
endpoints.

### `autonomy/route_learning.py`
`PathRunRecorder` records pose breadcrumbs during teleop (`start`, `add_pose`,
`stop`, `save`, `average_path`) so repeated laps can be averaged into a clean
route. **Called by** the cockpit while you drive a map.

### `autonomy/control_modes.py` — direct WASD (Feature 1)
Pure resolver (no driver, fully tested):
- `ControlMode {normal, flip, jump, backstand}`, `SpeedLevel {slow,normal,fast}`.
- `resolve_held(mode, speed, keys) -> ControlCommand` — NORMAL maps WASD/QE to
  a clamped velocity; other modes return noop (they're press-driven).
- `resolve_press(mode, key) -> ControlCommand` — flip→front/back/left/right
  flip, jump→forward jump only, backstand→noop, Space→stop.
- `mode_enter_action(mode)` — posture to enter (balance_stand / backstand).

**Called by** the cockpit's `/api/control/{mode,speed,keys,press}` handlers,
which then call the driver. Grounded in the real firmware capability table:
`Move` is the only continuous-velocity command; flips/jumps/backstand are
discrete one-shots.

---

## Targeting (Nerf) — `autonomy/targeting.py`
- `find_phone_users(observation)` — pairs a `cell phone` detection with the
  person box containing it, sorted by how centered they are.
- `TargetingController(nerf, tuning).step(observation) -> TargetingDecision`
  — aims (always safe) and fires only when **every** gate passes: armed +
  centered/locked for N frames + cooldown elapsed + under session cap.
- `NerfController`: `LoggingNerfController` (default, never actuates) and
  `SerialNerfController` (lazy pyserial, one trigger byte); `build_nerf_controller`.

**Called by** the `phone_tracker` workflow and the cockpit nerf endpoints.
Disarmed at construction and on shutdown — matches the writeup's interlock
philosophy.

---

## Planner + workflows

### `brain/local_llm.py` — optional LLM planner
`LocalRobotBrain(client, model)`: `handle(user_text)` sends the prompt +
tool schemas to Ollama, runs the **first** returned tool call against the
driver, and stops on any unknown/garbage/empty result. `repl()` is the typed
loop. The model is a *planner only* — it can request tools, it cannot touch
motors. **Called by** `main.py` (REPL) and the cockpit brain-prompt endpoint.
Not needed for driving.

### `autonomy/workflows.py` — routines, not prompts
`Workflow(name, steps=[Step(kind, params)])` interpreted by `WorkflowEngine`
(`register`, `start`, `stop`, `status`, `list_workflows`). Steps:
say/wait/stand/sit/stop/move/explore/scan/greet/greet_if_known/
scan_for_person/targeting/loop. Built-ins: `patrol_and_greet`, `find_person`,
`guard_post`, `phone_tracker`. **Called by** the cockpit workflow endpoints;
runs each step under a timeout and yields each loop iteration so it can't
starve the event loop.

---

## Web cockpits + viewers

### `ai_autonomy_gui.py` — the main cockpit (port 8775)
`AiAutonomyGui(...).run()` builds an aiohttp app behind bearer-token auth
(`auth.py`), connects the driver + brain + workflow engine + targeting +
perception, and serves the browser cockpit. Endpoints: `/video.mjpg`,
`/status.json`, `/api/manual/*`, `/api/control/*` (Feature 1 modes),
`/api/face/*`, `/api/maps/*`, `/api/autonomy/*`, `/api/workflow/*`,
`/api/nerf/*`, `/api/brain`. **Invoked:** `python -m go2_local_brain.ai_autonomy_gui`
(defaults to loopback + auto-generated token; `--bind-public`, `--auth-token`,
`--no-auth`, `--detector yolo`, `--nerf-backend serial`, etc.).

### `viewer.py` — read-only feed (port 8765)
`Go2BrowserViewer(robot_ip, host, port).run()` — MJPEG video + LiDAR view,
no control surface. `python -m go2_local_brain.viewer`.

### `face_viewer.py` — face page (port 8776)
`FaceViewer().run()` — video with live face-recognition overlays + enroll.
`python -m go2_local_brain.face_viewer [--backend ...] [--every N]`.

### `mode_gui.py` + thin wrappers
`ModeGui(host, port, mode).run()` with `make_main(mode, default_port)` powers
the focused single-purpose pages invoked by `ai_cli_video_gui`,
`ai_lidar_gui`, `ai_wasd_lidar_gui`, `wasd_video_gui` (each is just a `main()`
calling `make_main`).

### `legacy/gui.py`, `legacy/control_gui.py`
Superseded all-in-one GUIs kept for a lightweight page; `python -m
go2_local_brain.legacy.gui`. Not maintained.

---

## CLI tools, diagnostics, viz, auth

- `main.py` — `python -m go2_local_brain.main`: the LLM brain REPL.
- `diagnose_webrtc.py` / `diagnose_video.py` / `diagnose_motion.py` —
  `python -m go2_local_brain.diagnose_*`: probe the handshake / camera track /
  a single motion command and print exactly why it fails. First tool when
  "nothing works" (separates robot vs network vs compute, per the writeup).
- `recover_posture.py` — one-shot stand/recovery helper.
- `scripts/enroll_face.py` — enroll a face from a photo or live camera.
- `scripts/smoke_test_imports.py` — import sanity (`imports ok`).
- `viz/rerun_logger.py` — `RerunLogger.start()/log_text()/log_scalar()`:
  optional Rerun telemetry hooks (placeholder-light).
- `auth.py` — `generate_token()`, `make_auth_middleware(token)`,
  `inject_token(html, token)`: the bearer-token gate every cockpit `/api`
  POST passes through.

---

## End-to-end call chains (who calls what)

**Manual drive (normal mode):** browser key → `POST /api/control/keys` →
`resolve_held()` → `Go2WebRTCClient.move()` (clamp) → WebRTC `:9991` →
Unitree `sport_mode`/`mcf_main` → motors.

**Flip:** browser key-down → `POST /api/control/press` → `resolve_press()` →
`advanced_action("left_flip")` → MCF `LeftFlip` → motors.

**Autonomous patrol:** `POST /api/autonomy/activate` → `AutonomySupervisor.
step_once()` → `AutonomyNavigator.move_toward(waypoint)` (uses `LocalMapState`
pose + `LidarObstacleField` guard) → `move()` → robot; advances waypoint on
`scan_complete`.

**Face recognition:** WebRTC video frame → `YoloPerceptionProvider`/Haar boxes
→ `FaceIdentifier.identify_faces()` (`FaceDatabase` match) → `FaceTracker`
smoothing → browser overlay; "Save this face" → `enroll_from_image()` →
`FaceDatabase.save()`.

**LLM prompt (optional):** browser → `POST /api/brain` → `LocalRobotBrain.
handle()` → Ollama tool call → driver method (same clamps). The model never
bypasses the driver.
