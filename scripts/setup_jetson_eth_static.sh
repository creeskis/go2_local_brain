#!/usr/bin/env bash
set -euo pipefail

JETSON_IF="${JETSON_IF:-enP8p1s0}"
JETSON_IP="${JETSON_IP:-10.123.0.2/24}"
DOG_GATEWAY="${DOG_GATEWAY:-10.123.0.1}"

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
echo

run ip link set "$JETSON_IF" up
run ip addr flush dev "$JETSON_IF"
run ip addr add "$JETSON_IP" dev "$JETSON_IF"
run ip route replace default via "$DOG_GATEWAY"

echo
echo "Testing route:"
ping -c 3 "$DOG_GATEWAY" || true
ping -c 3 192.168.123.121 || true
ping -c 3 8.8.8.8 || true
echo
ip addr show "$JETSON_IF"
ip route
