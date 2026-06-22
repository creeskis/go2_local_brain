# Autonomous LiDAR patrol on the Jetson

A fully self-contained, headless patrol brain that runs **on the Jetson** (no
GUI, no Ollama/LLM). It connects to the Go2 over WebRTC, streams the LiDAR voxel
cloud, and continuously roams while avoiding obstacles. Supervised by systemd
(`go2-patrol.service`) and deployed over the host→dog→Jetson SSH bridge.

## How it works

```
LiDAR voxel cloud ── LidarObstacleField (front/left/right clearances by sector)
                          │
                  autonomy.patrol.PatrolController   (pure, unit-tested)
                          │  forward / steer / avoid / escape / hold
                          ▼
                  driver.move(vx, vy, vyaw, dt)  ── safety-clamped + deadman
```

- **Wander patrol:** cruise forward when the front sector is clear, creep + steer
  away in the caution band, back up + turn toward the more open side when
  blocked, and do a larger escape pivot if stuck in a corner.
- **LiDAR-gated:** the agent refuses to drive until LiDAR is fresh (override with
  `GO2_PATROL_ALLOW_BLIND=1`). The driver deadman halts the robot within ~1s if
  the loop ever stalls.
- **Pure planner:** all decision logic lives in `src/go2_local_brain/autonomy/patrol.py`
  and is covered by `tests/test_patrol.py` — no hardware needed to test it.

## Safety model

| Mode | `GO2_PATROL_ENABLE` | Behaviour |
|------|---------------------|-----------|
| **Dry run** (default) | `0` | Connects, streams LiDAR, **logs every decision, never moves** |
| **Live**  | `1` | Robot patrols autonomously |

The systemd unit defaults to **dry run**. Going live is an explicit choice
(`--live` on deploy, or `GO2_PATROL_ENABLE=1` in `~/.go2/env.local`). If the dog
carries the gun payload, confirm a clear area before going live.

## One-time setup (networking)

Do the dog-side bridge + Jetson networking from `deploy/jetson/README.md` first
(the Jetson must sit on `10.42.0.2` and reach the dog at `192.168.123.121`).

## Deploy from your laptop over the SSH bridge

The deploy script opens `127.0.0.1:10022 → dog → Jetson:22`, rsyncs this repo to
the Jetson, and runs the installer there. Needs `sshpass` + `rsync` on the host
and the gun-relay passwords in your private `.env`.

```bash
# Dry run (safe): connects + logs decisions, no motion.
GUN_DOG_PASSWORD=… GUN_JETSON_PASSWORD=… GUN_JETSON_SUDO_PASSWORD=… \
  bash scripts/deploy_patrol_to_jetson.sh

# Live: the robot will patrol.
GUN_DOG_PASSWORD=… GUN_JETSON_PASSWORD=… GUN_JETSON_SUDO_PASSWORD=… \
  bash scripts/deploy_patrol_to_jetson.sh --live
```

Follow it:

```bash
ssh -p 10022 unitree@127.0.0.1 'journalctl -u go2-patrol -f'
```

Stop / flip mode:

```bash
ssh -p 10022 unitree@127.0.0.1 'sudo systemctl stop go2-patrol'
ssh -p 10022 unitree@127.0.0.1 \
  "echo GO2_PATROL_ENABLE=1 >> ~/.go2/env.local && sudo systemctl restart go2-patrol"
```

## Install directly on the Jetson (alternative)

```bash
sudo bash deploy/jetson/install_patrol.sh                 # dry run
sudo env GO2_PATROL_GO_LIVE=1 bash deploy/jetson/install_patrol.sh   # live
```

## Jetson performance

`scripts/jetson_perf.sh` runs as an `ExecStartPre` (as root) and pins the board
to maximum sustained performance for consistent control-loop latency:

- `nvpmodel -m 0` — MAXN (override with `GO2_JETSON_NVP_MODE`, e.g. `1` for the
  25W sustained profile on JetPack 6.2 Super).
- `jetson_clocks` — disables DVFS down-clocking so CPU/GPU/EMC stay pinned.

It no-ops gracefully if the Jetson tools aren't present.

## Tuning (env vars, in `~/.go2/env.local`)

| Var | Default | Meaning |
|-----|---------|---------|
| `GO2_PATROL_ENABLE` | `0` | `1` = move; `0` = dry run |
| `GO2_PATROL_FORWARD_MPS` | `0.45` | cruise speed |
| `GO2_PATROL_STOP_M` | `0.55` | front clearance that triggers back-up + turn |
| `GO2_PATROL_SLOW_M` | `1.10` | front clearance that triggers creep + steer |
| `GO2_PATROL_TURN_RPS` | `0.85` | yaw rate while avoiding |
| `GO2_PATROL_ALLOW_BLIND` | `0` | `1` = roam even with no LiDAR (risky) |
| `GO2_PATROL_MAX_SECONDS` | `0` | auto-stop after N seconds (`0` = forever) |
| `GO2_JETSON_NVP_MODE` | `0` | nvpmodel power mode |

Run it by hand (dry run, 30s) for a smoke test:

```bash
~/.go2/venv/bin/python -m go2_local_brain.patrol_agent --max-seconds 30
```
