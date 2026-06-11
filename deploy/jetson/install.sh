#!/usr/bin/env bash
# Jetson Orin Nano one-shot installer for go2_local_brain.
#
# Run from the repo root as root (or with sudo):
#     sudo bash deploy/jetson/install.sh
#
# Safe to re-run; idempotent at each step. Failures stop the script with a
# clear message rather than half-installing.

set -euo pipefail

# -- config --------------------------------------------------------------------
GO2_USER="${SUDO_USER:-$USER}"
GO2_HOME="$(eval echo "~${GO2_USER}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${GO2_HOME}/.go2/venv"
ENV_FILE="${REPO_ROOT}/.env"
ENV_LOCAL="${GO2_HOME}/.go2/env.local"
ETHERNET_CONN_NAME="${GO2_ETH_CONN:-Wired connection 1}"
JETSON_IP="${GO2_JETSON_IP:-10.42.0.2/24}"
JETSON_GW="${GO2_JETSON_GATEWAY:-10.42.0.1}"

say() { printf "\n\033[1;36m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m==> WARN:\033[0m %s\n" "$*" >&2; }
die() { printf "\033[1;31m==> ERROR:\033[0m %s\n" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo bash $0"
[[ -d "${REPO_ROOT}/src/go2_local_brain" ]] \
    || die "expected repo at ${REPO_ROOT}; layout looks wrong"

# -- 1. apt prerequisites -----------------------------------------------------
say "1/6  apt prerequisites"
apt-get update
apt-get install -y \
    python3 python3-venv python3-pip git curl \
    portaudio19-dev ffmpeg ca-certificates iproute2

# -- 2. networking (10.42.0.2 on the wired link) ------------------------------
say "2/6  network: switch wired connection to ${JETSON_IP}"
if nmcli -t -f NAME connection show | grep -Fxq "${ETHERNET_CONN_NAME}"; then
    nmcli connection modify "${ETHERNET_CONN_NAME}" \
        ipv4.method manual \
        ipv4.addresses "${JETSON_IP}" \
        ipv4.gateway   "${JETSON_GW}" \
        ipv4.dns       "1.1.1.1 8.8.8.8" \
        ipv6.method ignore
    nmcli connection down "${ETHERNET_CONN_NAME}" || true
    nmcli connection up   "${ETHERNET_CONN_NAME}"
else
    warn "NetworkManager has no connection named '${ETHERNET_CONN_NAME}'."
    warn "Skip network config (you'll do it by hand) or rerun with GO2_ETH_CONN=<name>."
fi

# Quick reachability sanity check (don't fail if dog bridge isn't up yet).
if ping -c 2 -W 2 "${JETSON_GW}" >/dev/null 2>&1; then
    say "   dog gateway ${JETSON_GW} reachable"
else
    warn "dog gateway ${JETSON_GW} NOT reachable — did you install"
    warn "deploy/dog/jetson-bridge.service on the dog yet?"
fi

# -- 3. venv + pip install -----------------------------------------------------
say "3/6  venv + pip install -e ."
sudo -u "${GO2_USER}" mkdir -p "${GO2_HOME}/.go2"
if [[ ! -d "${VENV}" ]]; then
    sudo -u "${GO2_USER}" python3 -m venv "${VENV}"
fi
sudo -u "${GO2_USER}" "${VENV}/bin/pip" install --upgrade pip
sudo -u "${GO2_USER}" "${VENV}/bin/pip" install -e "${REPO_ROOT}"

# -- 4. .env --------------------------------------------------------------------
say "4/6  .env (skipping if existing)"
if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${REPO_ROOT}/.env.example" "${ENV_FILE}"
    chown "${GO2_USER}:${GO2_USER}" "${ENV_FILE}"
    say "   wrote ${ENV_FILE} from .env.example"
fi
if [[ ! -f "${ENV_LOCAL}" ]]; then
    sudo -u "${GO2_USER}" install -m 600 /dev/null "${ENV_LOCAL}"
    {
        echo "# Operator overrides for go2_local_brain. Loaded AFTER .env."
        echo "# Anything here wins. Safe to edit; not in git."
        echo "# Example: OLLAMA_MODEL=qwen3:8b"
    } > "${ENV_LOCAL}"
    chown "${GO2_USER}:${GO2_USER}" "${ENV_LOCAL}"
fi
# Ensure a stable GUI auth token exists so the browser URL is consistent
# across restarts. The systemd unit passes --auth-token ${GO2_GUI_TOKEN}.
if ! grep -q '^GO2_GUI_TOKEN=' "${ENV_LOCAL}" 2>/dev/null; then
    TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    echo "GO2_GUI_TOKEN=${TOKEN}" >> "${ENV_LOCAL}"
    say "   generated GUI auth token; browser URL: http://<jetson-ip>:8775/?token=${TOKEN}"
fi

# -- 5. Ollama -----------------------------------------------------------------
say "5/6  Ollama"
if ! command -v ollama >/dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
fi
# Service comes up via systemd automatically (see go2-ollama.service).

# -- 6. systemd units ----------------------------------------------------------
say "6/6  systemd units"
sed -e "s|@USER@|${GO2_USER}|g" \
    -e "s|@REPO@|${REPO_ROOT}|g" \
    -e "s|@VENV@|${VENV}|g" \
    -e "s|@HOME@|${GO2_HOME}|g" \
    "${REPO_ROOT}/deploy/jetson/go2-ollama.service.in"  > /etc/systemd/system/go2-ollama.service
sed -e "s|@USER@|${GO2_USER}|g" \
    -e "s|@REPO@|${REPO_ROOT}|g" \
    -e "s|@VENV@|${VENV}|g" \
    -e "s|@HOME@|${GO2_HOME}|g" \
    "${REPO_ROOT}/deploy/jetson/go2-brain.service.in"   > /etc/systemd/system/go2-brain.service

systemctl daemon-reload
systemctl enable --now go2-ollama.service
systemctl enable --now go2-brain.service

say "DONE. status check:"
systemctl --no-pager status go2-ollama || true
systemctl --no-pager status go2-brain  || true

cat <<EOF

Next:
  - Grab the GUI auth token + URL the brain printed on startup:
        journalctl -u go2-brain --no-pager | grep -E 'auth token|Browser URL'
  - Open that URL from your laptop browser.
  - To bump the model:
        echo 'OLLAMA_MODEL=qwen3:8b' > ${ENV_LOCAL}
        sudo systemctl restart go2-ollama go2-brain

Logs:
  journalctl -u go2-ollama -f
  journalctl -u go2-brain  -f

Uninstall:
  sudo bash ${REPO_ROOT}/deploy/jetson/uninstall.sh
EOF
