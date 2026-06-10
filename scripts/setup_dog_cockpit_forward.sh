#!/usr/bin/env bash
set -euo pipefail

DOG_ETH_IF="${DOG_ETH_IF:-eth0}"
DOG_WIFI_IF="${DOG_WIFI_IF:-wlan0}"
DOG_ETH_IP="${DOG_ETH_IP:-10.123.0.1/24}"
JETSON_IP="${JETSON_IP:-10.123.0.2}"
COCKPIT_PORT="${COCKPIT_PORT:-8775}"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run on the dog as root. This applies a narrow runtime port forward for the Jetson cockpit."
    exit 1
  fi
}

run() {
  echo "+ $*"
  "$@"
}

ensure_filter_rule() {
  local chain="$1"
  shift
  if iptables -C "$chain" "$@" >/dev/null 2>&1; then
    echo "filter rule exists: $chain $*"
  else
    run iptables -A "$chain" "$@"
  fi
}

need_root

echo "Dog cockpit port-forward setup"
echo "wifi=${DOG_WIFI_IF} eth=${DOG_ETH_IF} dog_eth_ip=${DOG_ETH_IP} jetson=${JETSON_IP} port=${COCKPIT_PORT}"
echo "This forwards only TCP ${COCKPIT_PORT}; it does not add general Jetson internet NAT."
echo

run ip link set "$DOG_ETH_IF" up
if ip addr show dev "$DOG_ETH_IF" | grep -q "${DOG_ETH_IP%/*}"; then
  echo "${DOG_ETH_IP} already present on ${DOG_ETH_IF}"
else
  run ip addr add "$DOG_ETH_IP" dev "$DOG_ETH_IF"
fi

run sysctl -w net.ipv4.ip_forward=1
run sysctl -w "net.ipv4.conf.${DOG_ETH_IF}.send_redirects=0"
run sysctl -w "net.ipv4.conf.${DOG_ETH_IF}.rp_filter=0"
run sysctl -w "net.ipv4.conf.${DOG_WIFI_IF}.rp_filter=0"

ensure_filter_rule FORWARD -i "$DOG_WIFI_IF" -o "$DOG_ETH_IF" -p tcp -d "$JETSON_IP" --dport "$COCKPIT_PORT" -j ACCEPT
ensure_filter_rule FORWARD -i "$DOG_ETH_IF" -o "$DOG_WIFI_IF" -p tcp -s "$JETSON_IP" --sport "$COCKPIT_PORT" -m state --state RELATED,ESTABLISHED -j ACCEPT

if iptables -t nat -C PREROUTING -i "$DOG_WIFI_IF" -p tcp --dport "$COCKPIT_PORT" -j DNAT --to-destination "${JETSON_IP}:${COCKPIT_PORT}" >/dev/null 2>&1; then
  echo "dnat rule exists"
else
  run iptables -t nat -A PREROUTING -i "$DOG_WIFI_IF" -p tcp --dport "$COCKPIT_PORT" -j DNAT --to-destination "${JETSON_IP}:${COCKPIT_PORT}"
fi

echo
echo "Laptop/WSL browser URL should be:"
echo "  http://192.168.123.121:${COCKPIT_PORT}"
echo
iptables -S
iptables -t nat -S
