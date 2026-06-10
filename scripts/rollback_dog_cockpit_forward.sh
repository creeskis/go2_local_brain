#!/usr/bin/env bash
set -euo pipefail

DOG_ETH_IF="${DOG_ETH_IF:-eth0}"
DOG_WIFI_IF="${DOG_WIFI_IF:-wlan0}"
JETSON_IP="${JETSON_IP:-10.123.0.2}"
COCKPIT_PORT="${COCKPIT_PORT:-8775}"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run on the dog as root. This removes the narrow Jetson cockpit port forward."
    exit 1
  fi
}

delete_filter_rule() {
  local chain="$1"
  shift
  if iptables -C "$chain" "$@" >/dev/null 2>&1; then
    echo "+ iptables -D $chain $*"
    iptables -D "$chain" "$@"
  else
    echo "filter rule not present: $chain $*"
  fi
}

need_root

delete_filter_rule FORWARD -i "$DOG_WIFI_IF" -o "$DOG_ETH_IF" -p tcp -d "$JETSON_IP" --dport "$COCKPIT_PORT" -j ACCEPT
delete_filter_rule FORWARD -i "$DOG_ETH_IF" -o "$DOG_WIFI_IF" -p tcp -s "$JETSON_IP" --sport "$COCKPIT_PORT" -m state --state RELATED,ESTABLISHED -j ACCEPT

if iptables -t nat -C PREROUTING -i "$DOG_WIFI_IF" -p tcp --dport "$COCKPIT_PORT" -j DNAT --to-destination "${JETSON_IP}:${COCKPIT_PORT}" >/dev/null 2>&1; then
  echo "+ iptables -t nat -D PREROUTING -i $DOG_WIFI_IF -p tcp --dport $COCKPIT_PORT -j DNAT --to-destination ${JETSON_IP}:${COCKPIT_PORT}"
  iptables -t nat -D PREROUTING -i "$DOG_WIFI_IF" -p tcp --dport "$COCKPIT_PORT" -j DNAT --to-destination "${JETSON_IP}:${COCKPIT_PORT}"
else
  echo "dnat rule not present"
fi

iptables -S
iptables -t nat -S
