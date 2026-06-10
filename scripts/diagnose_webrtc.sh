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

section "robot reachability"
run ping -c 3 "$DOG_ETH_GATEWAY"
run ping -c 3 "$ROBOT_WLAN_IP"
run ping -c 3 "$ROBOT_ETH_IP"
run ping -c 3 8.8.8.8

section "signaling reachability"
if command -v nc >/dev/null 2>&1; then
  run nc -vz "$ROBOT_WLAN_IP" 9991
  run nc -vz "$ROBOT_ETH_IP" 9991
else
  echo "nc not installed"
fi
run curl --max-time 5 "http://${ROBOT_WLAN_IP}:9991/con_notify"
run curl --max-time 5 "http://${ROBOT_ETH_IP}:9991/con_notify"

section "python package"
run python3 -m pip show unitree-webrtc-connect
run python3 -m pip show unitree_webrtc_connect

section "recommended app env"
echo "GO2_IP=${ROBOT_WLAN_IP}"
echo "GO2_WEBRTC_METHOD=LocalSTA"
echo "GO2_AES_128_KEY="
