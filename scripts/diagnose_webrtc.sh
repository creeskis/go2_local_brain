#!/usr/bin/env bash
set -u

ROBOT_WLAN_IP="${ROBOT_WLAN_IP:-192.168.123.121}"
ROBOT_ETH_IP="${ROBOT_ETH_IP:-192.168.123.161}"
DOG_ETH_GATEWAY="${DOG_ETH_GATEWAY:-10.123.0.1}"

section() {
  echo
  echo "## $1"
}

run() {
  echo "+ $*"
  "$@" 2>&1 || true
}

echo "Go2 WebRTC/network diagnostics"
echo "Run this on Jetson or WSL. On the dog, run the dog command block printed in docs/jetson_networking.md."

section "local addresses"
run ip addr
run ip route
run arp -a
for iface in enP8p1s0 eth0 wlan0; do
  if [[ -d "/sys/class/net/${iface}" ]]; then
    section "link ${iface}"
    run ip -br link show "$iface"
    if [[ -r "/sys/class/net/${iface}/carrier" ]]; then
      echo "carrier=$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo unknown)"
    fi
    if command -v ethtool >/dev/null 2>&1; then
      run ethtool "$iface"
    fi
  fi
done

section "robot reachability"
run ping -c 3 -W 2 "$DOG_ETH_GATEWAY"
run ping -c 3 -W 2 "$ROBOT_WLAN_IP"
run ping -c 3 -W 2 "$ROBOT_ETH_IP"
run ping -c 3 -W 2 8.8.8.8

section "signaling reachability"
if command -v nc >/dev/null 2>&1; then
  run nc -vz -w 3 "$DOG_ETH_GATEWAY" 9991
  run nc -vz -w 3 "$ROBOT_WLAN_IP" 9991
  run nc -vz -w 3 "$ROBOT_ETH_IP" 9991
else
  echo "nc not installed"
fi
run curl --max-time 5 "http://${DOG_ETH_GATEWAY}:9991/con_notify"
run curl --max-time 5 "http://${ROBOT_WLAN_IP}:9991/con_notify"
run curl --max-time 5 "http://${ROBOT_ETH_IP}:9991/con_notify"

section "python package"
run python3 -m pip show unitree-webrtc-connect
run python3 -m pip show unitree_webrtc_connect

section "recommended app env"
echo "GO2_IP=${ROBOT_WLAN_IP}"
echo "GO2_WEBRTC_METHOD=LocalSTA"
echo "GO2_AES_128_KEY="
echo
echo "If running on the Jetson Ethernet-only link and ${DOG_ETH_GATEWAY} is reachable, use:"
echo "GO2_IP=${DOG_ETH_GATEWAY}"
echo "GO2_WEBRTC_METHOD=LocalSTA"
