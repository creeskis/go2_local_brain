#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

ROBOT_IP="${GO2_IP:-192.168.123.121}"
GUI_HOST="${GO2_GUI_HOST:-127.0.0.1}"
GUI_PORT="${GO2_GUI_PORT:-8775}"
GUI_URL="http://127.0.0.1:${GUI_PORT}"
OLLAMA_URL="${OLLAMA_HOST:-http://127.0.0.1:11434}"

pass() { printf '[ OK ] %s\n' "$1"; }
warn() { printf '[WARN] %s\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1"; }

check_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    pass "command $1 found"
  else
    fail "command $1 missing"
  fi
}

check_http() {
  local name="$1"
  local url="$2"
  if curl -fsS --max-time 3 "$url" >/tmp/go2-doctor-http.out 2>/tmp/go2-doctor-http.err; then
    pass "$name reachable: $url"
  else
    warn "$name not reachable yet: $url"
  fi
}

echo "Go2 Jetson Doctor"
echo "repo=$ROOT_DIR"
echo "robot_ip=$ROBOT_IP"
echo "gui=${GUI_HOST}:${GUI_PORT}"
echo "ollama=$OLLAMA_URL"
echo

check_cmd python3
check_cmd curl
check_cmd git

if [[ -x ".venv/bin/python" ]]; then
  pass "virtualenv exists"
  .venv/bin/python scripts/smoke_test_imports.py >/dev/null 2>&1 && pass "package import ok" || warn "package import check failed"
else
  warn "virtualenv missing at .venv"
fi

if ping -c 1 -W 2 "$ROBOT_IP" >/dev/null 2>&1; then
  pass "robot ping ok"
else
  warn "robot ping failed; WebRTC may still work if ICMP is blocked"
fi

check_http "Ollama tags" "${OLLAMA_URL%/}/api/tags"
check_http "GUI health" "${GUI_URL}/api/health"
check_http "GUI status" "${GUI_URL}/status.json"
check_http "LiDAR debug" "${GUI_URL}/api/lidar/debug"

if command -v systemctl >/dev/null 2>&1; then
  systemctl is-enabled go2-local-brain >/dev/null 2>&1 && pass "systemd service enabled" || warn "systemd service not enabled"
  systemctl is-active go2-local-brain >/dev/null 2>&1 && pass "systemd service active" || warn "systemd service not active"
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null || warn "nvidia-smi failed"
else
  warn "nvidia-smi unavailable; Jetson may need tegrastats instead"
fi

echo
echo "Useful next checks:"
echo "  journalctl -u go2-local-brain -f"
echo "  curl ${GUI_URL}/api/health"
echo "  curl ${GUI_URL}/api/lidar/debug"
