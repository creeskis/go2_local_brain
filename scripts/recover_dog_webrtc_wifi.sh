#!/usr/bin/env sh
set -eu

# Run this on the robot as root after a reboot if LocalSTA WebRTC accepts
# /con_notify but closes before returning an SDP answer.

DOG_WIFI_IP="${DOG_WIFI_IP:-192.168.123.121}"
DOG_BAD_ETH_DHCP_IP="${DOG_BAD_ETH_DHCP_IP:-192.168.123.112}"
DOG_GATEWAY="${DOG_GATEWAY:-192.168.123.1}"
MASTER_LOG="${MASTER_LOG:-/tmp/unitree_webrtc_master.log}"
SIGNAL_LOG="${SIGNAL_LOG:-/tmp/unitree_xfxton.log}"

require_root() {
  if [ "$(id -u)" != "0" ]; then
    echo "Run this on the dog as root." >&2
    exit 1
  fi
}

run() {
  echo "+ $*"
  "$@"
}

try() {
  echo "+ $*"
  "$@" 2>/dev/null || true
}

require_root

echo "Dog WebRTC Wi-Fi recovery"
echo "Before:"
date -u || true
ip route || true
cat /proc/sys/net/ipv4/ip_forward || true

if [ "${DOG_UTC_DATE:-}" != "" ]; then
  run date -u -s "$DOG_UTC_DATE"
fi

echo "Disabling forwarding and removing reboot-restored eth0 route overlap..."
run sh -c "echo 0 > /proc/sys/net/ipv4/ip_forward"
try ip addr del "$DOG_BAD_ETH_DHCP_IP/24" dev eth0
try ip route del default via "$DOG_GATEWAY" dev eth0
try ip route del 192.168.123.0/24 dev eth0
try ip link set eth0 down

echo "Restarting Unitree WebRTC bridge bound to Wi-Fi..."
try pkill -f xfkTon
try pkill -f unitreeWebRTCClientMaster
sleep 2

echo "+ nohup /unitree/module/webrtc_bridge/bin/unitreeWebRTCClientMaster --enable_multi_session true > ${MASTER_LOG} 2>&1 &"
nohup /unitree/module/webrtc_bridge/bin/unitreeWebRTCClientMaster --enable_multi_session true >"${MASTER_LOG}" 2>&1 &
sleep 3
echo "+ nohup /unitree/module/webrtc_bridge/src/webrtc_dds_bridge/xfkTon > ${SIGNAL_LOG} 2>&1 &"
nohup /unitree/module/webrtc_bridge/src/webrtc_dds_bridge/xfkTon >"${SIGNAL_LOG}" 2>&1 &
sleep 5

echo "After:"
date -u || true
ip route || true
cat /proc/sys/net/ipv4/ip_forward || true
ss -lntup | grep -E '9991|9990|webrtc|unitree' || true

echo "Local con_notify check:"
curl --max-time 5 "http://127.0.0.1:9991/con_notify" >/dev/null
echo "OK: xfkTon answered on 127.0.0.1:9991"

echo "Expected WebRTC UDP bind should be ${DOG_WIFI_IP}:<port> or 0.0.0.0:<port>, not eth0."
echo "If the next SDK test fails, read:"
echo "  tail -n 120 ${SIGNAL_LOG}"
echo "  tail -n 120 ${MASTER_LOG}"
