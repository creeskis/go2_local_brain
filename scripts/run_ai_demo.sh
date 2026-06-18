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

export GO2_IP="${GO2_IP:-192.168.123.121}"
export GO2_WEBRTC_METHOD="${GO2_WEBRTC_METHOD:-LocalSTA}"
export GO2_AES_128_KEY="${GO2_AES_128_KEY:-}"
export FORCE_MOTION_MODE="${FORCE_MOTION_MODE:-normal}"
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"

OLLAMA_PID=""

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ -n "$OLLAMA_PID" ]] && kill -0 "$OLLAMA_PID" >/dev/null 2>&1; then
    kill "$OLLAMA_PID" >/dev/null 2>&1 || true
    wait "$OLLAMA_PID" >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

ollama_url="http://${OLLAMA_HOST}/api/tags"
if command -v ollama >/dev/null 2>&1; then
  if ! curl -fsS --max-time 1 "$ollama_url" >/dev/null 2>&1; then
    ollama serve >"${OLLAMA_LOG_FILE:-/tmp/go2_ollama_demo.log}" 2>&1 &
    OLLAMA_PID=$!
    for _ in $(seq 1 40); do
      curl -fsS --max-time 1 "$ollama_url" >/dev/null 2>&1 && break
      sleep 0.25
    done
  fi
else
  echo "warning: ollama not found; AI commands will fail until ollama is installed" >&2
fi

python -m go2_local_brain.ai_wasd_lidar_gui --host "${GO2_AI_DEMO_HOST:-127.0.0.1}" --port "${GO2_AI_DEMO_PORT:-8778}" "$@"
