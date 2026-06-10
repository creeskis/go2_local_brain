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

HOST="${GO2_GUI_HOST:-0.0.0.0}"
PORT="${GO2_GUI_PORT:-8775}"
MAPS_DIR="${GO2_MAPS_DIR:-maps}"
START_MAP="${GO2_START_MAP:-}"
DETECTOR="${GO2_DETECTOR:-yolo}"
YOLO_MODEL="${GO2_YOLO_MODEL:-yolov8n.pt}"
YOLO_THRESHOLD="${GO2_YOLO_THRESHOLD:-0.55}"
YOLO_DEVICE="${GO2_YOLO_DEVICE:-0}"
FOLLOW_SOURCE="${GO2_FOLLOW_SOURCE:-visual-or-sound}"

ARGS=(
  --host "$HOST"
  --port "$PORT"
  --maps-dir "$MAPS_DIR"
  --detector "$DETECTOR"
  --yolo-model "$YOLO_MODEL"
  --yolo-threshold "$YOLO_THRESHOLD"
  --follow-source "$FOLLOW_SOURCE"
)

if [[ -n "$START_MAP" ]]; then
  ARGS+=(--map "$START_MAP")
fi

if [[ -n "$YOLO_DEVICE" ]]; then
  ARGS+=(--yolo-device "$YOLO_DEVICE")
fi

if [[ "${GO2_FACE_DETECTION:-1}" != "0" ]]; then
  ARGS+=(--face-detection)
fi

if [[ "${GO2_ALLOW_NO_DETECTOR:-0}" == "1" ]]; then
  ARGS+=(--allow-no-detector)
fi

exec python -m go2_local_brain.ai_autonomy_gui "${ARGS[@]}"
