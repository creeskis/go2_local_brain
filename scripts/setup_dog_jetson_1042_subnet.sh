#!/usr/bin/env bash
set -euo pipefail

DOG_ETH_IF="${DOG_ETH_IF:-eth0}"
DOG_WIFI_IF="${DOG_WIFI_IF:-wlan0}"
DOG_ETH_IP="${DOG_ETH_IP:-10.42.0.1/24}"
JETSON_CIDR="${JETSON_CIDR:-10.42.0.0/24}"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run on the dog as root. This script applies runtime ip/iptables changes only." >&2
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

ensure_nat_rule() {
  if iptables -t nat -C POSTROUTING -s "$JETSON_CIDR" -o "$DOG_WIFI_IF" -j MASQUERADE >/dev/null 2>&1; then
    echo "nat rule exists: POSTROUTING -s ${JETSON_CIDR} -o ${DOG_WIFI_IF} -j MASQUERADE"
  else
    run iptables -t nat -A POSTROUTING -s "$JETSON_CIDR" -o "$DOG_WIFI_IF" -j MASQUERADE
  fi
}

need_root

echo "Dog Jetson 10.42 subnet setup"
echo "eth=${DOG_ETH_IF} wifi=${DOG_WIFI_IF} dog_eth_ip=${DOG_ETH_IP} jetson_cidr=${JETSON_CIDR}"
echo "This keeps the existing dog eth0 192.168.123.161 address and only adds the 10.42 bridge subnet."
echo

run ip link set "$DOG_ETH_IF" up

if ip addr show dev "$DOG_ETH_IF" | grep -q "${DOG_ETH_IP%/*}"; then
  echo "${DOG_ETH_IP} already present on ${DOG_ETH_IF}"
else
  run ip addr add "$DOG_ETH_IP" dev "$DOG_ETH_IF"
fi

echo
echo "Enable runtime IPv4 forwarding"
run sh -c "echo 1 > /proc/sys/net/ipv4/ip_forward"

echo
echo "Install idempotent forwarding/NAT rules"
ensure_nat_rule
ensure_filter_rule FORWARD -i "$DOG_ETH_IF" -s "$JETSON_CIDR" -o "$DOG_WIFI_IF" -j ACCEPT
ensure_filter_rule FORWARD -i "$DOG_WIFI_IF" -o "$DOG_ETH_IF" -d "$JETSON_CIDR" -m state --state RELATED,ESTABLISHED -j ACCEPT

echo
echo "Current dog network state:"
ip -br addr show "$DOG_ETH_IF" "$DOG_WIFI_IF"
ip route
echo "ip_forward=$(cat /proc/sys/net/ipv4/ip_forward)"
iptables -S FORWARD
iptables -t nat -S POSTROUTING

echo
echo "Done. Jetson should use an address inside ${JETSON_CIDR}, usually 10.42.0.2/24, with gateway ${DOG_ETH_IP%/*}."
