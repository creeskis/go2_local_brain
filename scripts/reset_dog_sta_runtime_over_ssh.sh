#!/usr/bin/env bash
set -euo pipefail

# Run this from WSL. It copies the dog-side STA runtime reset script to the dog
# and executes it as root over SSH.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOG_HOST="${GO2_IP:-192.168.123.121}"
DOG_USER="${GO2_DOG_USER:-root}"
REMOTE_SCRIPT="${GO2_DOG_RESET_SCRIPT:-/tmp/go2_reset_dog_sta_runtime.sh}"

DOG_ETH_IF="${DOG_ETH_IF:-eth0}"
DOG_WIFI_IF="${DOG_WIFI_IF:-wlan0}"
DOG_GATEWAY="${DOG_GATEWAY:-192.168.123.1}"
DOG_REMOVE_PRIMARY_ETH_STA="${DOG_REMOVE_PRIMARY_ETH_STA:-0}"
DOG_SET_ETH_DOWN="${DOG_SET_ETH_DOWN:-0}"
DOG_RESET_WEBRTC="${DOG_RESET_WEBRTC:-1}"
COCKPIT_PORT="${COCKPIT_PORT:-8775}"

echo "Dog STA runtime reset over SSH"
echo "Target: ${DOG_USER}@${DOG_HOST}"
echo "Remote script: ${REMOTE_SCRIPT}"

scp "${ROOT_DIR}/scripts/reset_dog_sta_runtime.sh" "${DOG_USER}@${DOG_HOST}:${REMOTE_SCRIPT}"

ssh "${DOG_USER}@${DOG_HOST}" \
  "chmod +x '${REMOTE_SCRIPT}' && DOG_ETH_IF='${DOG_ETH_IF}' DOG_WIFI_IF='${DOG_WIFI_IF}' DOG_WIFI_IP='${DOG_HOST}' DOG_GATEWAY='${DOG_GATEWAY}' DOG_REMOVE_PRIMARY_ETH_STA='${DOG_REMOVE_PRIMARY_ETH_STA}' DOG_SET_ETH_DOWN='${DOG_SET_ETH_DOWN}' DOG_RESET_WEBRTC='${DOG_RESET_WEBRTC}' COCKPIT_PORT='${COCKPIT_PORT}' '${REMOTE_SCRIPT}'"

echo
echo "Next WSL test:"
echo "  GO2_AES_128_KEY= GO2_IP=${DOG_HOST} GO2_WEBRTC_METHOD=LocalSTA VERBOSE_WEBRTC_LOGS=1 python -m go2_local_brain.diagnose_webrtc"
echo
echo "If eth0 still steals 192.168.123.0/24, rerun with:"
echo "  DOG_REMOVE_PRIMARY_ETH_STA=1 ./scripts/reset_dog_sta_runtime_over_ssh.sh"
