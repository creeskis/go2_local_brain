#!/usr/bin/env bash
set -euo pipefail

DOG_ETH_IF="${DOG_ETH_IF:-eth0}"
DOG_WIFI_IF="${DOG_WIFI_IF:-wlan0}"
DOG_ETH_IP="${DOG_ETH_IP:-10.123.0.1/24}"
JETSON_CIDR="${JETSON_CIDR:-10.123.0.0/24}"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run on the dog as root. This removes only the runtime rules added by setup_dog_jetson_route.sh."
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

echo "Rolling back dog Jetson route runtime changes"
delete_filter_rule FORWARD -i "$DOG_ETH_IF" -o "$DOG_WIFI_IF" -j ACCEPT
delete_filter_rule FORWARD -i "$DOG_WIFI_IF" -o "$DOG_ETH_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT
if iptables -t nat -C POSTROUTING -s "$JETSON_CIDR" -o "$DOG_WIFI_IF" -j MASQUERADE >/dev/null 2>&1; then
  echo "+ iptables -t nat -D POSTROUTING -s $JETSON_CIDR -o $DOG_WIFI_IF -j MASQUERADE"
  iptables -t nat -D POSTROUTING -s "$JETSON_CIDR" -o "$DOG_WIFI_IF" -j MASQUERADE
else
  echo "nat rule not present"
fi

if ip addr show dev "$DOG_ETH_IF" | grep -q "${DOG_ETH_IP%/*}"; then
  echo "+ ip addr del $DOG_ETH_IP dev $DOG_ETH_IF"
  ip addr del "$DOG_ETH_IP" dev "$DOG_ETH_IF"
fi

echo "Leaving net.ipv4.ip_forward unchanged; set it manually if needed:"
echo "  sysctl -w net.ipv4.ip_forward=0"
echo
ip addr show "$DOG_ETH_IF"
iptables -S
iptables -t nat -S
