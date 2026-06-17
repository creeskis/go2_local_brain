#!/usr/bin/env bash
set -euo pipefail

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing dependency: $1" >&2
    exit 127
  fi
}

need expect

: "${GUN_DOG_PASSWORD:?set GUN_DOG_PASSWORD in .env}"
: "${GUN_JETSON_PASSWORD:?set GUN_JETSON_PASSWORD in .env}"
export GUN_JETSON_SUDO_PASSWORD="${GUN_JETSON_SUDO_PASSWORD:-$GUN_JETSON_PASSWORD}"

export GUN_DOG_HOST="${GUN_DOG_HOST:-192.168.123.121}"
export GUN_DOG_USER="${GUN_DOG_USER:-root}"
export GUN_JETSON_HOST="${GUN_JETSON_HOST:-10.42.0.2}"
export GUN_JETSON_USER="${GUN_JETSON_USER:-unitree}"
export GUN_FIRE_COMMAND="${GUN_FIRE_COMMAND:-cat /dev/ttyUSB0 | xxd}"
export GUN_STOP_COMMAND="${GUN_STOP_COMMAND:-printf '\\x30' > /dev/ttyUSB0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec expect -f "$SCRIPT_DIR/gun_session_manual.expect"
