#!/usr/bin/env sh
set -eu

# Run this on the dog as root when STA keeps an address but LocalSTA WebRTC
# does not work. It removes repo-added bridge/forwarding traces and stale eth0
# overlap so a fresh STA attempt can be made from the host computer.

DOG_ETH_IF="${DOG_ETH_IF:-eth0}"
DOG_WIFI_IF="${DOG_WIFI_IF:-wlan0}"
DOG_WIFI_IP="${DOG_WIFI_IP:-192.168.123.121}"
DOG_GATEWAY="${DOG_GATEWAY:-192.168.123.1}"
DOG_STALE_ETH_IPS="${DOG_STALE_ETH_IPS:-192.168.123.112/24 10.42.0.1/24 10.123.0.1/24}"
DOG_REMOVE_PRIMARY_ETH_STA="${DOG_REMOVE_PRIMARY_ETH_STA:-0}"
DOG_SET_ETH_DOWN="${DOG_SET_ETH_DOWN:-0}"
DOG_RESET_WEBRTC="${DOG_RESET_WEBRTC:-1}"
COCKPIT_PORT="${COCKPIT_PORT:-8775}"
MASTER_LOG="${MASTER_LOG:-/tmp/unitree_webrtc_master_reset.log}"
SIGNAL_LOG="${SIGNAL_LOG:-/tmp/unitree_xfxton_reset.log}"

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

delete_iptables_rule() {
  table="$1"
  chain="$2"
  shift 2
  while iptables -t "$table" -C "$chain" "$@" 2>/dev/null; do
    try iptables -t "$table" -D "$chain" "$@"
  done
}

print_state() {
  echo "Addresses:"
  ip -br addr show "$DOG_ETH_IF" "$DOG_WIFI_IF" 2>/dev/null || true
  echo
  echo "Routes:"
  ip route || true
  echo
  echo "ip_forward:"
  cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || true
  echo
  echo "Repo-related iptables rules:"
  iptables -t nat -S 2>/dev/null | grep -E '10\.42\.0\.0|10\.123\.0\.0|10\.42\.0\.2|10\.123\.0\.2|MASQUERADE|DNAT' || true
  iptables -S 2>/dev/null | grep -E '10\.42\.0\.0|10\.123\.0\.0|10\.42\.0\.2|10\.123\.0\.2' || true
  echo
  echo "WebRTC listeners/processes:"
  ss -lntup 2>/dev/null | grep -E '9991|9990|webrtc|unitree' || true
}

stop_repo_service() {
  unit="$1"
  if command -v systemctl >/dev/null 2>&1; then
    try systemctl disable --now "$unit"
    try rm -f "/etc/systemd/system/$unit"
    try systemctl daemon-reload
  fi
}

restart_webrtc() {
  echo
  echo "Restarting Unitree WebRTC bridge..."
  try pkill -f xfkTon
  try pkill -f unitreeWebRTCClientMaster
  sleep 2

  echo "+ nohup /unitree/module/webrtc_bridge/bin/unitreeWebRTCClientMaster --enable_multi_session true > ${MASTER_LOG} 2>&1 &"
  nohup /unitree/module/webrtc_bridge/bin/unitreeWebRTCClientMaster --enable_multi_session true >"${MASTER_LOG}" 2>&1 &
  sleep 3

  echo "+ nohup /unitree/module/webrtc_bridge/src/webrtc_dds_bridge/xfkTon > ${SIGNAL_LOG} 2>&1 &"
  nohup /unitree/module/webrtc_bridge/src/webrtc_dds_bridge/xfkTon >"${SIGNAL_LOG}" 2>&1 &
  sleep 5
}

require_root

echo "Dog STA runtime reset"
echo "Target Wi-Fi IP: ${DOG_WIFI_IP}"
echo
echo "Before reset:"
print_state

echo
echo "Stopping repo-installed dog bridge services..."
stop_repo_service jetson-bridge.service

echo
echo "Removing repo temp scripts from the dog..."
try rm -f /tmp/go2_setup_jetson_1042_subnet.sh
try rm -f /tmp/go2_setup_dog_cockpit_forward.sh
try rm -f /tmp/go2_recover_webrtc_wifi.sh
try rm -f /tmp/go2_reset_dog_sta_runtime.sh

echo
echo "Removing Jetson/forwarding iptables traces..."
delete_iptables_rule nat POSTROUTING -s 10.42.0.0/24 -o "$DOG_WIFI_IF" -j MASQUERADE
delete_iptables_rule filter FORWARD -i "$DOG_ETH_IF" -s 10.42.0.0/24 -o "$DOG_WIFI_IF" -j ACCEPT
delete_iptables_rule filter FORWARD -i "$DOG_WIFI_IF" -o "$DOG_ETH_IF" -d 10.42.0.0/24 -m state --state RELATED,ESTABLISHED -j ACCEPT
delete_iptables_rule nat POSTROUTING -s 10.123.0.0/24 -o "$DOG_WIFI_IF" -j MASQUERADE
delete_iptables_rule filter FORWARD -i "$DOG_ETH_IF" -s 10.123.0.0/24 -o "$DOG_WIFI_IF" -j ACCEPT
delete_iptables_rule filter FORWARD -i "$DOG_WIFI_IF" -o "$DOG_ETH_IF" -d 10.123.0.0/24 -m state --state RELATED,ESTABLISHED -j ACCEPT
delete_iptables_rule nat PREROUTING -i "$DOG_WIFI_IF" -p tcp --dport "$COCKPIT_PORT" -j DNAT --to-destination "10.42.0.2:${COCKPIT_PORT}"
delete_iptables_rule nat PREROUTING -i "$DOG_WIFI_IF" -p tcp --dport "$COCKPIT_PORT" -j DNAT --to-destination "10.123.0.2:${COCKPIT_PORT}"
delete_iptables_rule filter FORWARD -i "$DOG_WIFI_IF" -o "$DOG_ETH_IF" -p tcp -d 10.42.0.2 --dport "$COCKPIT_PORT" -j ACCEPT
delete_iptables_rule filter FORWARD -i "$DOG_WIFI_IF" -o "$DOG_ETH_IF" -p tcp -d 10.123.0.2 --dport "$COCKPIT_PORT" -j ACCEPT

echo
echo "Disabling Linux forwarding..."
try sh -c "echo 0 > /proc/sys/net/ipv4/ip_forward"

echo
echo "Removing stale eth0 addresses and route overlap..."
for ip_addr in $DOG_STALE_ETH_IPS; do
  try ip addr del "$ip_addr" dev "$DOG_ETH_IF"
done

if [ "$DOG_REMOVE_PRIMARY_ETH_STA" = "1" ]; then
  try ip addr del 192.168.123.161/24 dev "$DOG_ETH_IF"
fi

try ip route del default via "$DOG_GATEWAY" dev "$DOG_ETH_IF"
try ip route del 192.168.123.0/24 dev "$DOG_ETH_IF"
try ip route del 10.42.0.0/24 dev "$DOG_ETH_IF"
try ip route del 10.123.0.0/24 dev "$DOG_ETH_IF"

if [ "$DOG_SET_ETH_DOWN" = "1" ] || [ "$DOG_REMOVE_PRIMARY_ETH_STA" = "1" ]; then
  try ip link set "$DOG_ETH_IF" down
fi

try ip link set "$DOG_WIFI_IF" up

if [ "$DOG_RESET_WEBRTC" = "1" ]; then
  restart_webrtc
fi

echo
echo "After reset:"
print_state

echo
echo "Local WebRTC signaling check:"
if curl --max-time 5 "http://127.0.0.1:9991/con_notify" >/dev/null 2>&1; then
  echo "OK: xfkTon answered on 127.0.0.1:9991"
else
  echo "WARN: xfkTon did not answer on 127.0.0.1:9991"
  echo "Read logs:"
  echo "  tail -n 120 ${SIGNAL_LOG}"
  echo "  tail -n 120 ${MASTER_LOG}"
fi

echo
echo "Done. Use GO2_IP=${DOG_WIFI_IP} GO2_WEBRTC_METHOD=LocalSTA from WSL for the next fresh test."
