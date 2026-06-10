#!/usr/bin/env bash
set -euo pipefail

DOG_ETH_IF="${DOG_ETH_IF:-eth0}"
DOG_WIFI_IF="${DOG_WIFI_IF:-wlan0}"
DOG_ETH_IP="${DOG_ETH_IP:-10.123.0.1/24}"
JETSON_CIDR="${JETSON_CIDR:-10.123.0.0/24}"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run on the dog as root. This script only applies runtime ip/iptables changes."
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

echo "Dog Jetson route setup"
echo "eth=${DOG_ETH_IF} wifi=${DOG_WIFI_IF} dog_eth_ip=${DOG_ETH_IP} jetson_cidr=${JETSON_CIDR}"
echo "This intentionally does not change wlan0, WebRTC services, or persistent boot config."
echo

run ip link set "$DOG_ETH_IF" up
if ip addr show dev "$DOG_ETH_IF" | grep -q "${DOG_ETH_IP%/*}"; then
  echo "${DOG_ETH_IP} already present on ${DOG_ETH_IF}"
else
  run ip addr add "$DOG_ETH_IP" dev "$DOG_ETH_IF"
fi

echo
echo "Dog Ethernet link state:"
ip -br link show "$DOG_ETH_IF"
if [[ -r "/sys/class/net/${DOG_ETH_IF}/carrier" ]]; then
  echo "carrier=$(cat "/sys/class/net/${DOG_ETH_IF}/carrier")"
fi

run sysctl -w net.ipv4.ip_forward=1
run sysctl -w "net.ipv4.conf.${DOG_ETH_IF}.send_redirects=0"
run sysctl -w "net.ipv4.conf.${DOG_ETH_IF}.rp_filter=0"
ensure_filter_rule FORWARD -i "$DOG_ETH_IF" -o "$DOG_WIFI_IF" -j ACCEPT
ensure_filter_rule FORWARD -i "$DOG_WIFI_IF" -o "$DOG_ETH_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT
if iptables -t nat -C POSTROUTING -s "$JETSON_CIDR" -o "$DOG_WIFI_IF" -j MASQUERADE >/dev/null 2>&1; then
  echo "nat rule exists"
else
  run iptables -t nat -A POSTROUTING -s "$JETSON_CIDR" -o "$DOG_WIFI_IF" -j MASQUERADE
fi

echo
echo "Current dog network state:"
ip addr show "$DOG_ETH_IF"
ip route
iptables -S
iptables -t nat -S
