#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

export GO2_IP="${GO2_IP:-192.168.123.161}"
export GO2_WEBRTC_METHOD="${GO2_WEBRTC_METHOD:-LocalSTA}"
export GO2_AES_128_KEY="${GO2_AES_128_KEY:-}"
export FORCE_MOTION_MODE="${FORCE_MOTION_MODE:-normal}"

export GUN_DOG_HOST="${GUN_DOG_HOST:-192.168.123.121}"
export GUN_DOG_USER="${GUN_DOG_USER:-root}"
export GUN_DOG_PASSWORD="${GUN_DOG_PASSWORD:-}"
export GUN_JETSON_HOST="${GUN_JETSON_HOST:-10.42.0.2}"
export GUN_JETSON_USER="${GUN_JETSON_USER:-unitree}"
export GUN_JETSON_PASSWORD="${GUN_JETSON_PASSWORD:-}"
export GUN_FIRE_COMMAND="${GUN_FIRE_COMMAND:-cat /dev/ttyUSB0 | xxd}"
export GUN_STOP_COMMAND="${GUN_STOP_COMMAND:-printf '\\x30' > /dev/ttyUSB0}"

exec python -m go2_local_brain.local_cockpit --host "${GO2_GUI_HOST:-127.0.0.1}" --port "${GO2_GUI_PORT:-8775}"
