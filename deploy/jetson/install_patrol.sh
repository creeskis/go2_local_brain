#!/usr/bin/env bash
# On-Jetson installer for the headless autonomy services. No Ollama, no GUI.
#
#   sudo bash deploy/jetson/install_patrol.sh
#
# Installs BOTH systemd units (go2-autonomy + go2-patrol) and enables ONE of them
# (only one may drive the robot). Choose with GO2_JETSON_AGENT:
#   autonomy (default) = video stream + LiDAR roam + person follow
#   patrol             = LiDAR roam only
#
# Networking (10.42.0.2 + dog NAT) is assumed already set up; see README.md.
# Set GO2_PATROL_GO_LIVE=1 to flip from dry-run to live motion. Idempotent.
#   sudo env GO2_JETSON_AGENT=autonomy GO2_PATROL_GO_LIVE=1 bash deploy/jetson/install_patrol.sh
set -euo pipefail

GO2_USER="${SUDO_USER:-${GO2_PATROL_USER:-unitree}}"
GO2_HOME="$(eval echo "~${GO2_USER}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${GO2_HOME}/.go2/venv"
ENV_LOCAL="${GO2_HOME}/.go2/env.local"
GO_LIVE="${GO2_PATROL_GO_LIVE:-0}"
AGENT="${GO2_JETSON_AGENT:-autonomy}"

case "${AGENT}" in
    autonomy) UNIT="go2-autonomy"; OTHER_UNIT="go2-patrol";   ENABLE_VAR="GO2_AUTONOMY_ENABLE" ;;
    patrol)   UNIT="go2-patrol";   OTHER_UNIT="go2-autonomy"; ENABLE_VAR="GO2_PATROL_ENABLE" ;;
    *) echo "ERROR: GO2_JETSON_AGENT must be 'autonomy' or 'patrol', got '${AGENT}'" >&2; exit 2 ;;
esac

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
# NOTE: person-follow needs a YOLO detector (ultralytics + a Jetson torch build).
# Install those separately; without them the agent still streams video + roams.

say "3/4  env.local + perms"
if [[ ! -f "${ENV_LOCAL}" ]]; then
    sudo -u "${GO2_USER}" install -m 600 /dev/null "${ENV_LOCAL}"
    {
        echo "# go2 autonomy overrides (loaded AFTER .env; this file wins)."
        echo "GO2_IP=192.168.123.121"
        echo "# GPU detector for person-follow on the Jetson:"
        echo "# GO2_DETECTOR_DEVICE=cuda"
        echo "# Uncomment to actually move the robot (or deploy with --live):"
        echo "# ${ENABLE_VAR}=1"
    } >> "${ENV_LOCAL}"
    chown "${GO2_USER}:${GO2_USER}" "${ENV_LOCAL}"
fi
if [[ "${GO_LIVE}" == "1" ]]; then
    if ! grep -q "^${ENABLE_VAR}=1" "${ENV_LOCAL}"; then
        echo "${ENABLE_VAR}=1" >> "${ENV_LOCAL}"
    fi
    say "   LIVE mode requested: ${ENABLE_VAR}=1 (robot WILL move)"
fi
chmod +x "${REPO_ROOT}/scripts/jetson_perf.sh"

say "4/4  systemd units (agent=${AGENT})"
for u in go2-autonomy go2-patrol; do
    sed -e "s|@USER@|${GO2_USER}|g" \
        -e "s|@REPO@|${REPO_ROOT}|g" \
        -e "s|@VENV@|${VENV}|g" \
        -e "s|@HOME@|${GO2_HOME}|g" \
        "${REPO_ROOT}/deploy/jetson/${u}.service.in" > "/etc/systemd/system/${u}.service"
done
systemctl daemon-reload
# Only one agent drives the robot: disable the other, enable + (re)start the chosen one.
systemctl disable --now "${OTHER_UNIT}.service" 2>/dev/null || true
systemctl enable "${UNIT}.service"
systemctl restart "${UNIT}.service"

say "DONE. status:"
systemctl --no-pager status "${UNIT}" || true
cat <<EOF

Installed agent: ${AGENT}  (${UNIT}.service)
  Mode:  $([[ "${GO_LIVE}" == "1" ]] && echo "LIVE (robot moves)" || echo "DRY RUN (no motion)")
  Logs:  journalctl -u ${UNIT} -f
  Go live later:  echo '${ENABLE_VAR}=1' >> ${ENV_LOCAL} && sudo systemctl restart ${UNIT}
  Stop:  sudo systemctl stop ${UNIT}
$([[ "${AGENT}" == "autonomy" ]] && echo "  Video:  watch from the laptop via an SSH local-forward of port 8788 (see PATROL.md)")
EOF
