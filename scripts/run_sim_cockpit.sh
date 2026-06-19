#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

export GO2_FACE_BACKEND="${GO2_FACE_BACKEND:-null}"
export GO2_FACE_ENABLED="${GO2_FACE_ENABLED:-1}"
export GO2_FACE_DETECT_MAX_WIDTH="${GO2_FACE_DETECT_MAX_WIDTH:-360}"
export GO2_JPEG_QUALITY="${GO2_JPEG_QUALITY:-68}"

exec python -m go2_local_brain.sim_cockpit \
  --host "${GO2_SIM_HOST:-127.0.0.1}" \
  --port "${GO2_SIM_PORT:-8785}" \
  --camera "${GO2_SIM_CAMERA:-0}" \
  --fps "${GO2_SIM_FPS:-12}"
