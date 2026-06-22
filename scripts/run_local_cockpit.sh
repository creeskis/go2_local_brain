#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

load_env_file() {
  local file="$1"
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    line="${line#export }"
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$file"
}

if [[ -f ".env" ]]; then
  load_env_file ".env"
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
export GUN_JETSON_SUDO_PASSWORD="${GUN_JETSON_SUDO_PASSWORD:-$GUN_JETSON_PASSWORD}"
export GUN_LOCAL_SSH_PORT="${GUN_LOCAL_SSH_PORT:-10022}"
export GUN_LOG_FILE="${GUN_LOG_FILE:-/tmp/go2_gun_relay.log}"
export GUN_REMOTE_LOG_FILE="${GUN_REMOTE_LOG_FILE:-/tmp/go2_gun_remote.log}"
export GUN_TUNNEL_SCRIPT="${GUN_TUNNEL_SCRIPT:-scripts/gun_tunnel_manual.sh}"
export GUN_COMMAND_SCRIPT="${GUN_COMMAND_SCRIPT:-scripts/gun_command_manual.sh}"
if [[ "${GUN_FIRE_COMMAND:-}" == "sudo bash -lc 'cat /dev/ttyUSB0 | xxd'" ]]; then
  GUN_FIRE_COMMAND="cat /dev/ttyUSB0 | xxd"
fi
if [[ "${GUN_STOP_COMMAND:-}" == "sudo bash -lc 'printf \"\\x30\" > /dev/ttyUSB0'" ]]; then
  GUN_STOP_COMMAND="printf '\\x30' > /dev/ttyUSB0"
fi
export GUN_FIRE_COMMAND="${GUN_FIRE_COMMAND:-cat /dev/ttyUSB0 | xxd}"
export GUN_STOP_COMMAND="${GUN_STOP_COMMAND:-printf '\\x30' > /dev/ttyUSB0}"
export GO2_FACE_BACKEND="${GO2_FACE_BACKEND:-insightface}"
export GO2_FACE_DETECTOR="${GO2_FACE_DETECTOR:-yolo}"
export GO2_FACE_INTERVAL_S="${GO2_FACE_INTERVAL_S:-0.20}"
export GO2_FACE_DETECT_MAX_WIDTH="${GO2_FACE_DETECT_MAX_WIDTH:-960}"
export GO2_FACE_YOLO_CONFIDENCE="${GO2_FACE_YOLO_CONFIDENCE:-0.20}"
export GO2_FACE_YOLO_IMAGE_SIZE="${GO2_FACE_YOLO_IMAGE_SIZE:-960}"
export GO2_FACE_YOLO_DEVICE="${GO2_FACE_YOLO_DEVICE:-}"
export GO2_FACE_INSIGHT_DET_SIZE="${GO2_FACE_INSIGHT_DET_SIZE:-640}"
export GO2_FACE_MAX_RESULTS="${GO2_FACE_MAX_RESULTS:-16}"
export GO2_JPEG_QUALITY="${GO2_JPEG_QUALITY:-80}"
export GO2_FOLLOW_ENABLED="${GO2_FOLLOW_ENABLED:-1}"
export GO2_FOLLOW_YOLO_MODEL="${GO2_FOLLOW_YOLO_MODEL:-}"
export GO2_FOLLOW_YOLO_THRESHOLD="${GO2_FOLLOW_YOLO_THRESHOLD:-0.32}"
export GO2_FOLLOW_YOLO_DEVICE="${GO2_FOLLOW_YOLO_DEVICE:-}"
export GO2_FOLLOW_INTERVAL_S="${GO2_FOLLOW_INTERVAL_S:-0.20}"
export GO2_FOLLOW_TARGET_HEIGHT="${GO2_FOLLOW_TARGET_HEIGHT:-0.80}"
export GO2_FOLLOW_MAX_FORWARD="${GO2_FOLLOW_MAX_FORWARD:-1.15}"
export GO2_FOLLOW_MAX_TURN="${GO2_FOLLOW_MAX_TURN:-0.55}"
export GO2_FOLLOW_MOVE_DURATION="${GO2_FOLLOW_MOVE_DURATION:-0.45}"

if [[ "$GO2_FACE_DETECTOR" == "yolo" && -z "${GO2_FACE_YOLO_MODEL:-}" ]]; then
  FACE_MODEL_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/go2_local_brain/models"
  GO2_FACE_YOLO_MODEL="$FACE_MODEL_DIR/yolov8n-face.pt"
  if [[ ! -f "$GO2_FACE_YOLO_MODEL" ]]; then
    mkdir -p "$FACE_MODEL_DIR"
    echo "Downloading YOLO face model (one time)..."
    curl -fL "https://github.com/akanametov/yolo-face/releases/download/1.0.0/yolov8n-face.pt" \
      -o "$GO2_FACE_YOLO_MODEL.tmp"
    printf '%s  %s\n' \
      'd545bf1add5aa736a4febac4f4f9245a6d596cd0fe70d5d57989fe0cb9e626ca' \
      "$GO2_FACE_YOLO_MODEL.tmp" | sha256sum -c -
    mv "$GO2_FACE_YOLO_MODEL.tmp" "$GO2_FACE_YOLO_MODEL"
  fi
  export GO2_FACE_YOLO_MODEL
fi

if [[ "$GO2_FOLLOW_ENABLED" == "1" && -z "$GO2_FOLLOW_YOLO_MODEL" ]]; then
  FOLLOW_MODEL_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/go2_local_brain/models"
  GO2_FOLLOW_YOLO_MODEL="$FOLLOW_MODEL_DIR/yolov8n.pt"
  if [[ ! -f "$GO2_FOLLOW_YOLO_MODEL" ]]; then
    mkdir -p "$FOLLOW_MODEL_DIR"
    echo "Downloading YOLO person model (one time)..."
    curl -fL "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt" \
      -o "$GO2_FOLLOW_YOLO_MODEL.tmp"
    printf '%s  %s\n' \
      'f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36' \
      "$GO2_FOLLOW_YOLO_MODEL.tmp" | sha256sum -c -
    mv "$GO2_FOLLOW_YOLO_MODEL.tmp" "$GO2_FOLLOW_YOLO_MODEL"
  fi
  export GO2_FOLLOW_YOLO_MODEL
fi

exec python -m go2_local_brain.local_cockpit --host "${GO2_GUI_HOST:-127.0.0.1}" --port "${GO2_GUI_PORT:-8775}"
