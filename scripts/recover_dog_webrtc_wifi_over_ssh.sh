#!/usr/bin/env bash
set -euo pipefail

# Run this from the WSL instance. It copies the dog-side recovery script and
# syncs the dog clock from this machine. By default it does not restart robot
# services. Set GO2_FORCE_WEBRTC_RESTART=1 only if signaling is wedged and you
# accept the risk of disrupting the motion stack.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOG_HOST="${GO2_IP:-192.168.123.121}"
DOG_USER="${GO2_DOG_USER:-root}"
REMOTE_SCRIPT="${GO2_DOG_RECOVERY_SCRIPT:-/tmp/go2_recover_webrtc_wifi.sh}"
UTC_NOW="$(date -u '+%Y-%m-%d %H:%M:%S')"

echo "Dog WebRTC Wi-Fi recovery over SSH"
echo "Target: ${DOG_USER}@${DOG_HOST}"
echo "UTC: ${UTC_NOW}"

scp "${ROOT_DIR}/scripts/recover_dog_webrtc_wifi.sh" "${DOG_USER}@${DOG_HOST}:${REMOTE_SCRIPT}"
ssh "${DOG_USER}@${DOG_HOST}" "chmod +x '${REMOTE_SCRIPT}' && DOG_UTC_DATE='${UTC_NOW}' DOG_WIFI_IP='${DOG_HOST}' '${REMOTE_SCRIPT}'"

echo
echo "Now test from WSL. If the dog WebRTC bridge is bound to eth0, use GO2_IP=192.168.123.161:"
echo "  GO2_AES_128_KEY= GO2_IP=${DOG_HOST} GO2_WEBRTC_METHOD=LocalSTA VERBOSE_WEBRTC_LOGS=1 python -m go2_local_brain.diagnose_webrtc"
