#!/usr/bin/env bash
set -euo pipefail

JETSON_IF="${JETSON_IF:-enP8p1s0}"
JETSON_IP="${JETSON_IP:-10.123.0.2/24}"
DOG_GATEWAY="${DOG_GATEWAY:-10.123.0.1}"
JETSON_SET_DEFAULT="${JETSON_SET_DEFAULT:-0}"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run on the Jetson with sudo. This applies runtime interface settings only."
    exit 1
  fi
}

run() {
  echo "+ $*"
  "$@"
}

need_root

echo "Jetson Ethernet runtime setup"
echo "interface=${JETSON_IF} ip=${JETSON_IP} gateway=${DOG_GATEWAY}"
echo "This avoids 192.168.123.0/24 overlap on the dog Ethernet link."
echo "Default route via dog is disabled unless JETSON_SET_DEFAULT=1."
echo

run ip link set "$JETSON_IF" up
run ip addr flush dev "$JETSON_IF"
run ip addr add "$JETSON_IP" dev "$JETSON_IF"
run ip route replace 192.168.123.0/24 via "$DOG_GATEWAY" dev "$JETSON_IF"
if [[ "$JETSON_SET_DEFAULT" == "1" ]]; then
  run ip route replace default via "$DOG_GATEWAY"
fi

echo
echo "Link state:"
ip -br link show "$JETSON_IF"
if [[ -r "/sys/class/net/${JETSON_IF}/carrier" ]]; then
  echo "carrier=$(cat "/sys/class/net/${JETSON_IF}/carrier")"
fi
if command -v ethtool >/dev/null 2>&1; then
  ethtool "$JETSON_IF" | grep -E 'Link detected|Speed|Duplex' || true
fi

echo
echo "Testing route:"
ping -c 3 -W 2 "$DOG_GATEWAY" || true
ping -c 3 -W 2 192.168.123.121 || true
if [[ "$JETSON_SET_DEFAULT" == "1" ]]; then
  ping -c 3 -W 2 8.8.8.8 || true
fi
echo
ip addr show "$JETSON_IF"
ip route
