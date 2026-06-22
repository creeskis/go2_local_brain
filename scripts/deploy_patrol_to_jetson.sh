#!/usr/bin/env bash
# Deploy + (re)start the autonomous patrol service ON THE JETSON, over the
# host -> dog -> Jetson SSH bridge (same path the gun relay uses).
#
#   GUN_DOG_PASSWORD=...  GUN_JETSON_PASSWORD=...  [GUN_JETSON_SUDO_PASSWORD=...] \
#     bash scripts/deploy_patrol_to_jetson.sh [--live]
#
# It opens 127.0.0.1:10022 -> dog -> Jetson:22, rsyncs this repo to the Jetson,
# then runs deploy/jetson/install_patrol.sh there (venv + pip + systemd unit).
#
#   (no flag)  install/refresh in DRY RUN  -> connects + logs decisions, NO motion
#   --live     write GO2_PATROL_ENABLE=1   -> the robot WILL patrol autonomously
#
# Requires sshpass + rsync on the host. Passwords come from the same env vars as
# the gun relay; put them in your private .env, never commit them.
set -euo pipefail

LIVE=0
if [[ "${1:-}" == "--live" ]]; then LIVE=1; fi

DOG_HOST="${GUN_DOG_HOST:-192.168.123.121}"
DOG_USER="${GUN_DOG_USER:-root}"
JET_HOST="${GUN_JETSON_HOST:-10.42.0.2}"
JET_USER="${GUN_JETSON_USER:-unitree}"
PORT="${GUN_LOCAL_SSH_PORT:-10022}"
REMOTE_DIR="${GO2_JETSON_REPO:-/home/${JET_USER}/robotics/go2_local_brain}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1" >&2; exit 127; }; }
need sshpass
need rsync
need ssh

: "${GUN_DOG_PASSWORD:?set GUN_DOG_PASSWORD (dog root password)}"
: "${GUN_JETSON_PASSWORD:?set GUN_JETSON_PASSWORD (jetson ${JET_USER} password)}"
SUDO_PW="${GUN_JETSON_SUDO_PASSWORD:-$GUN_JETSON_PASSWORD}"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

echo "==> opening SSH bridge  host -> ${DOG_HOST} -> ${JET_HOST}:22  on 127.0.0.1:${PORT}"
SSHPASS="$GUN_DOG_PASSWORD" sshpass -e ssh "${SSH_OPTS[@]}" -N \
  -o ExitOnForwardFailure=yes \
  -L "127.0.0.1:${PORT}:${JET_HOST}:22" "${DOG_USER}@${DOG_HOST}" &
TUNNEL_PID=$!
trap 'kill "$TUNNEL_PID" 2>/dev/null || true' EXIT

echo "==> waiting for the tunnel to come up"
ok=0
for _ in $(seq 1 40); do
  if SSHPASS="$GUN_JETSON_PASSWORD" sshpass -e ssh "${SSH_OPTS[@]}" -p "$PORT" "${JET_USER}@127.0.0.1" true 2>/dev/null; then
    ok=1; break
  fi
  sleep 0.5
done
[[ "$ok" == "1" ]] || { echo "ERROR: could not reach the Jetson through the bridge" >&2; exit 1; }

echo "==> rsync repo -> ${JET_USER}@jetson:${REMOTE_DIR}"
SSHPASS="$GUN_JETSON_PASSWORD" sshpass -e rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude '.venv-win' --exclude '__pycache__' \
  --exclude '*.pyc' --exclude 'outputs' --exclude 'maps/*.json' \
  -e "ssh ${SSH_OPTS[*]} -p ${PORT}" \
  "${REPO_ROOT}/" "${JET_USER}@127.0.0.1:${REMOTE_DIR}/"

echo "==> remote install (sudo) + service (re)start  [live=${LIVE}]"
# The sudo password is fed on stdin (here-string) to `sudo -S`; sshpass -e takes
# the SSH login password from $SSHPASS, so it leaves stdin free for sudo. One
# sudo authenticates, then install_patrol.sh runs entirely as root.
SSHPASS="$GUN_JETSON_PASSWORD" sshpass -e ssh "${SSH_OPTS[@]}" -p "$PORT" "${JET_USER}@127.0.0.1" \
  "cd '${REMOTE_DIR}' && sudo -S -p '' env GO2_PATROL_GO_LIVE='${LIVE}' bash deploy/jetson/install_patrol.sh" \
  <<< "${SUDO_PW}"

echo
if [[ "$LIVE" == "1" ]]; then
  echo "==> LIVE: the Jetson is now patrolling autonomously. Stop with:"
  echo "    ssh -p ${PORT} ${JET_USER}@127.0.0.1 'sudo systemctl stop go2-patrol'"
else
  echo "==> DRY RUN deployed (no motion). Re-run with --live to patrol."
fi
echo "    Follow logs: ssh -p ${PORT} ${JET_USER}@127.0.0.1 'journalctl -u go2-patrol -f'"
