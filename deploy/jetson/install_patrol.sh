#!/usr/bin/env bash
# On-Jetson installer for the autonomous LiDAR patrol service. No Ollama, no GUI.
#
#   sudo bash deploy/jetson/install_patrol.sh
#
# Networking (10.42.0.2 + dog NAT) is assumed already configured; see
# deploy/jetson/README.md for that one-time setup. Idempotent and safe to re-run.
#
# Set GO2_PATROL_GO_LIVE=1 to flip the service from dry-run to live patrol:
#   sudo env GO2_PATROL_GO_LIVE=1 bash deploy/jetson/install_patrol.sh
set -euo pipefail

GO2_USER="${SUDO_USER:-${GO2_PATROL_USER:-unitree}}"
GO2_HOME="$(eval echo "~${GO2_USER}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${GO2_HOME}/.go2/venv"
ENV_LOCAL="${GO2_HOME}/.go2/env.local"
GO_LIVE="${GO2_PATROL_GO_LIVE:-0}"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m==> ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo bash $0"
[[ -d "${REPO_ROOT}/src/go2_local_brain" ]] || die "repo layout looks wrong at ${REPO_ROOT}"

say "1/4  apt prerequisites"
apt-get update
apt-get install -y python3 python3-venv python3-pip git curl ca-certificates iproute2 ffmpeg

say "2/4  venv + editable install"
sudo -u "${GO2_USER}" mkdir -p "${GO2_HOME}/.go2"
[[ -d "${VENV}" ]] || sudo -u "${GO2_USER}" python3 -m venv "${VENV}"
sudo -u "${GO2_USER}" "${VENV}/bin/pip" install --upgrade pip
sudo -u "${GO2_USER}" "${VENV}/bin/pip" install -e "${REPO_ROOT}"

say "3/4  env.local + perms"
if [[ ! -f "${ENV_LOCAL}" ]]; then
    sudo -u "${GO2_USER}" install -m 600 /dev/null "${ENV_LOCAL}"
    {
        echo "# go2 patrol overrides (loaded AFTER .env; this file wins)."
        echo "GO2_IP=192.168.123.121"
        echo "# Uncomment to actually move the robot (or deploy with --live):"
        echo "# GO2_PATROL_ENABLE=1"
    } >> "${ENV_LOCAL}"
    chown "${GO2_USER}:${GO2_USER}" "${ENV_LOCAL}"
fi
if [[ "${GO_LIVE}" == "1" ]]; then
    if ! grep -q '^GO2_PATROL_ENABLE=1' "${ENV_LOCAL}"; then
        echo 'GO2_PATROL_ENABLE=1' >> "${ENV_LOCAL}"
    fi
    say "   LIVE mode requested: GO2_PATROL_ENABLE=1 (robot WILL patrol)"
fi
chmod +x "${REPO_ROOT}/scripts/jetson_perf.sh"

say "4/4  systemd unit go2-patrol"
sed -e "s|@USER@|${GO2_USER}|g" \
    -e "s|@REPO@|${REPO_ROOT}|g" \
    -e "s|@VENV@|${VENV}|g" \
    -e "s|@HOME@|${GO2_HOME}|g" \
    "${REPO_ROOT}/deploy/jetson/go2-patrol.service.in" > /etc/systemd/system/go2-patrol.service
systemctl daemon-reload
systemctl enable go2-patrol.service
systemctl restart go2-patrol.service

say "DONE. status:"
systemctl --no-pager status go2-patrol || true
cat <<EOF

Patrol service installed.
  Mode:   $([[ "${GO_LIVE}" == "1" ]] && echo "LIVE (robot moves)" || echo "DRY RUN (no motion)")
  Logs:   journalctl -u go2-patrol -f
  Go live later:  echo 'GO2_PATROL_ENABLE=1' >> ${ENV_LOCAL} && sudo systemctl restart go2-patrol
  Stop:   sudo systemctl stop go2-patrol
EOF
