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

exec python -m go2_local_brain.ai_autonomy_gui \
  --host "${GO2_AI_HOST:-127.0.0.1}" \
  --port "${GO2_AI_PORT:-8777}" \
  --maps-dir "${GO2_AI_MAPS_DIR:-maps}" \
  --allow-no-detector \
  "$@"
