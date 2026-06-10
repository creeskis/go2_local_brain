#!/usr/bin/env bash
# Reverse of install.sh — removes systemd units and the static IP.
# Leaves the venv, the .env, and the apt packages in place.

set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root: sudo bash $0" >&2; exit 1; }

ETHERNET_CONN_NAME="${GO2_ETH_CONN:-Wired connection 1}"

echo "==> stopping + removing services"
for s in go2-brain go2-ollama; do
    systemctl disable --now "${s}.service" 2>/dev/null || true
    rm -f "/etc/systemd/system/${s}.service"
done
systemctl daemon-reload

echo "==> restoring DHCP on wired link (${ETHERNET_CONN_NAME})"
if nmcli -t -f NAME connection show | grep -Fxq "${ETHERNET_CONN_NAME}"; then
    nmcli connection modify "${ETHERNET_CONN_NAME}" \
        ipv4.method auto \
        ipv4.addresses "" \
        ipv4.gateway "" \
        ipv4.dns ""
    nmcli connection down "${ETHERNET_CONN_NAME}" || true
    nmcli connection up   "${ETHERNET_CONN_NAME}"
fi

echo "==> done. Repo + venv are untouched; remove ~/.go2 if you want a clean wipe."
