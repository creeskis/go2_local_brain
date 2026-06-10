#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="${GO2_SERVICE_NAME:-go2-local-brain}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
USER_NAME="${GO2_SERVICE_USER:-$(id -un)}"
GROUP_NAME="${GO2_SERVICE_GROUP:-$(id -gn)}"

TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

sed \
  -e "s|User=jetson|User=${USER_NAME}|g" \
  -e "s|Group=jetson|Group=${GROUP_NAME}|g" \
  -e "s|/home/jetson/robotics/go2_local_brain|${ROOT_DIR}|g" \
  "$ROOT_DIR/deploy/systemd/go2-local-brain.service" > "$TMP_FILE"

sudo install -m 0644 "$TMP_FILE" "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "Installed ${SERVICE_FILE}"
echo "Start it with: sudo systemctl start ${SERVICE_NAME}"
echo "Watch logs with: journalctl -u ${SERVICE_NAME} -f"
