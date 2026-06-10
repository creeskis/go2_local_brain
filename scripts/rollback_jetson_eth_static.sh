#!/usr/bin/env bash
set -euo pipefail

JETSON_IF="${JETSON_IF:-enP8p1s0}"
JETSON_IP="${JETSON_IP:-10.123.0.2/24}"
DOG_GATEWAY="${DOG_GATEWAY:-10.123.0.1}"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run on the Jetson with sudo. This removes runtime settings added by setup_jetson_eth_static.sh."
    exit 1
  fi
}

need_root

echo "Rolling back Jetson Ethernet runtime settings"
if ip addr show dev "$JETSON_IF" | grep -q "${JETSON_IP%/*}"; then
  echo "+ ip addr del $JETSON_IP dev $JETSON_IF"
  ip addr del "$JETSON_IP" dev "$JETSON_IF"
else
  echo "$JETSON_IP not present on $JETSON_IF"
fi

if ip route show 192.168.123.0/24 | grep -q "$DOG_GATEWAY"; then
  echo "+ ip route del 192.168.123.0/24 via $DOG_GATEWAY dev $JETSON_IF"
  ip route del 192.168.123.0/24 via "$DOG_GATEWAY" dev "$JETSON_IF" || true
fi

if ip route show default | grep -q "$DOG_GATEWAY"; then
  echo "+ ip route del default via $DOG_GATEWAY"
  ip route del default via "$DOG_GATEWAY" || true
fi

ip addr show "$JETSON_IF"
ip route
