# Face ID, workflows, and phone-user targeting

Three features added on top of the existing YOLO perception:

1. **Face identification** — recognize and label enrolled people.
2. **Workflows** — named routines the robot runs without prompting each step.
3. **Phone-user targeting + Nerf** — track people on their phone and (when
   armed) fire the foam-dart launcher.

All three are **off by default** and safe to import without ML/serial deps —
the heavy backends are lazy-imported only when actually used.

---

## 1. Face identification

### How it fits

The perception layer already produces *face boxes* (OpenCV Haar). Face ID
adds *identity* on top: it embeds each face crop into a vector and matches it
against a database of enrolled people.

- `autonomy/face_id.py` — `FaceEmbedder` (pluggable), `FaceDatabase` (JSON),
  `FaceIdentifier` (glue).
- `autonomy/face_tracker.py` — stable track IDs + majority-vote label
  smoothing across frames.

### Backends

| backend | dim | install | notes |
|---|---|---|---|
| `insightface` | 512 | `pip install insightface onnxruntime` (or `onnxruntime-gpu`) | best on Jetson GPU |
| `face_recognition` | 128 | `pip install face_recognition` (needs cmake + dlib) | simple CPU path |
| `null` (default) | – | none | embeds nothing; everything is "unknown" |

### Enrolling faces

```bash
# From a photo:
python scripts/enroll_face.py --label cooper --image cooper.jpg --backend insightface

# From the live robot camera (grabs 5 frames, enrolls the largest face):
python scripts/enroll_face.py --label cooper --camera --shots 5
```

The database lives at `~/.config/go2_local_brain/faces.json` (override with
`$GO2_FACE_DB`). Re-running with the same label adds samples and improves
robustness. Enroll 2-3 people, a few shots each.

### Matching

`FaceDatabase.identify(embedding)` returns the best-scoring label above the
match threshold, else `"unknown"`. Tune the threshold per backend — dlib and
ArcFace cosines have different scales.

---

## 2. Workflows

A **workflow** is a named list of steps the robot runs until it finishes or
you stop it — so you "activate a mode" instead of prompting every action.

- `autonomy/workflows.py` — `Workflow`, `Step`, `WorkflowEngine`.

### Built-in workflows

| name | what it does |
|---|---|
| `patrol_and_greet` | roam the room; greet anyone the robot recognizes |
| `find_person` | rotate to find a person, then stop and watch |
| `guard_post` | stand guard; periodically greet known faces |
| `phone_tracker` | track phone users and (if armed) fire the Nerf launcher |

### Running them

In the AI autonomy GUI right rail there's a **Workflows** panel: pick one,
"Run workflow", "Stop workflow". Or via HTTP:

```bash
curl -X POST http://<host>:8775/api/workflow/start \
     -H 'Authorization: Bearer <token>' \
     -H 'Content-Type: application/json' \
     -d '{"name":"patrol_and_greet"}'

curl -X POST http://<host>:8775/api/workflow/stop -H 'Authorization: Bearer <token>'
```

### Step kinds

`say`, `wait`, `stand`, `sit`, `stop`, `move`, `explore`, `scan`, `greet`,
`greet_if_known`, `scan_for_person`, `targeting`, `loop`. Every step runs
under a 20-second timeout, and `loop` yields each iteration so an
all-synchronous loop can't peg the CPU or starve WebRTC.

Define your own by constructing a `Workflow(name, description, steps=[...])`
and `engine.register(workflow)`.

---

## 3. Phone-user targeting + Nerf

The `phone_tracker` workflow plus `autonomy/targeting.py` implement
"track people using their phone, aim, and fire the Nerf launcher".

### Safety model

Firing is gated behind **every** one of these:

- the Nerf controller is **armed** (disarmed at construction / on shutdown),
- a phone-using person target is found (a `cell phone` detection inside a
  `person` box),
- that target has stayed **centered** for N consecutive frames (locked),
- a per-shot **cooldown** has elapsed,
- the per-session **fire cap** hasn't been reached.

**Aiming** (turning to center a target) is always safe and runs regardless of
arm state. Only the actual trigger is gated.

### Nerf backends

| backend | actuates? | when |
|---|---|---|
| `logging` (default) | **no** — logs "would fire" | always safe; testing |
| `serial` | writes a trigger byte to the Arduino | `--nerf-backend serial` |

The Arduino firmware (foam dart launcher) is out of scope here. The serial
backend just writes one byte (`b"F"`) to `/dev/ttyACM0` @ 115200; the Arduino
does the rest. Until you wire that and pass `--nerf-backend serial`, nothing
physically fires.

### Using it

```bash
# Start the GUI with the (still-disarmed) serial backend:
python -m go2_local_brain.ai_autonomy_gui --host 0.0.0.0 --nerf-backend serial

# In the GUI: select "phone_tracker", Run workflow. The robot will aim at
# phone users but NOT fire.
# Then, deliberately, press ARM in the Nerf panel. Now a centered + locked
# phone user gets a dart, subject to cooldown + session cap.
```

Disarm at any time (button, or `POST /api/nerf/disarm`); shutdown disarms
automatically.

### Tuning

`TargetingTuning` controls `center_tolerance`, `lock_frames`, `cooldown_s`,
`session_fire_cap`, and the aim gain/sign. The aim `yaw_sign` defaults to the
same hardware-inverted convention as follow mode; flip it if the robot turns
the wrong way.
