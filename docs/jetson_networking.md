# Jetson, Dog Ethernet, And WebRTC Networking

This runbook is for the Go2 Air layout where the Jetson has Ethernet only and is plugged into the robot's Ethernet port.

## Known Addresses

| Device | Interface | Address |
| --- | --- | --- |
| Go2 Air | wlan0 | `192.168.123.121` |
| Go2 Air | eth0 | `192.168.123.161` plus optional `10.123.0.1/24` |
| Jetson | enP8p1s0 | `10.123.0.2/24` |
| Laptop/WSL | Wi-Fi side | can reach `192.168.123.121` |

Do not put the Jetson Ethernet interface on `192.168.123.0/24` while the dog also has wlan0 and eth0 in that subnet. That creates ambiguous ARP and route selection. The clean temporary Ethernet link is:

```text
Jetson enP8p1s0: 10.123.0.2/24
Go2 eth0:        10.123.0.1/24
```

## Recommended WebRTC Target

When the Jetson is only connected through the dog Ethernet link, use the dog Ethernet-side `10.123.0.1` address:

```env
GO2_IP=10.123.0.1
GO2_WEBRTC_METHOD=LocalSTA
```

When running from WSL/laptop over Wi-Fi, use the robot wlan0 address:

```env
GO2_IP=192.168.123.121
GO2_WEBRTC_METHOD=LocalSTA
```

`unitree_webrtc_connect` supports `LocalAP`, `LocalSTA`, and `Remote`. For this repo, `LocalSTA` is the correct default for a robot already joined to the local network. `LocalAP` is for the robot's direct Wi-Fi AP mode. `Remote` is the Unitree cloud/TURN path and needs account credentials.

If this hangs from the Jetson:

```bash
ping -c 3 -W 2 10.123.0.1
```

stop testing WebRTC. The Jetson cannot reach the dog Ethernet interface yet. Check cable, carrier, dog `eth0` address, and Jetson `enP8p1s0` address first.

## Why WebRTC Failed After Bridging

The routing test proved that packet forwarding worked:

```text
Jetson -> 10.123.0.1 -> dog wlan0 NAT -> robot/operator network
```

But WebRTC signaling is not just a normal HTTP reachability check. The SDK's LocalSTA handshake posts to the robot signaling service, usually `http://ROBOT_IP:9991/con_notify`, and expects an SDP answer. If the dog has two interfaces in the same subnet, or if NAT/iptables changes cause the robot bridge to pick a different source/interface, the signaling service can accept the TCP request and then close without SDP. That appears as:

```text
NoSdpAnswerError: Robot signaling returned no SDP answer
RemoteDisconnected('Remote end closed connection without response')
```

An empty response from `curl http://ROBOT_IP:9991` is not by itself a failure. The useful check is:

```bash
curl http://192.168.123.121:9991/con_notify
```

If `/con_notify` returns a large blob, the service is alive. If the SDK still gets no SDP, suspect a wedged WebRTC bridge, method mismatch, another client, or interface/routing confusion.

## Safer Browser Access To The Jetson

The laptop cannot directly reach Jetson Ethernet when the Jetson is only plugged into the dog. Prefer one of these:

### Option A: SSH Reverse Tunnel From Jetson To WSL

Use this when the Jetson can reach the WSL/laptop SSH server over the dog route.

```bash
ssh -N -R 8775:127.0.0.1:8775 USER@LAPTOP_OR_WSL_IP
```

Then open on the laptop:

```text
http://127.0.0.1:8775
```

This exposes only the cockpit port and does not require dog-side port forwarding.

### Option B: USB Wi-Fi Or USB Ethernet Adapter On Jetson

This is the cleanest long-term answer. Give the Jetson a direct operator-network interface, leave the dog Ethernet as `10.123.0.2/24`, and avoid using the robot as the operator network bridge.

### Option C: Narrow Cockpit Port Forward

This is the preferred Ethernet-only setup when no USB Wi-Fi adapter is available:

```text
Jetson browser cockpit: 10.123.0.2:8775
Dog wlan0 forward:      192.168.123.121:8775 -> 10.123.0.2:8775
Dog WebRTC target:      10.123.0.1
```

It avoids broad Jetson internet NAT and forwards only the browser cockpit port.

On the dog:

```bash
sudo ./scripts/setup_dog_cockpit_forward.sh
```

On the Jetson:

```bash
sudo ./scripts/setup_jetson_eth_static.sh
```

In Jetson `.env`:

```env
GO2_IP=10.123.0.1
GO2_WEBRTC_METHOD=LocalSTA
GO2_GUI_HOST=0.0.0.0
GO2_GUI_PORT=8775
```

From the laptop/WSL browser:

```text
http://192.168.123.121:8775
```

Rollback:

```bash
sudo ./scripts/rollback_dog_cockpit_forward.sh
sudo ./scripts/rollback_jetson_eth_static.sh
```

### Option D: Temporary Dog NAT For Jetson Internet

Use the scripts in this repo only for runtime testing:

On the dog:

```bash
sudo ./scripts/setup_dog_jetson_route.sh
```

On the Jetson:

```bash
sudo ./scripts/setup_jetson_eth_static.sh
```

Rollback on the dog:

```bash
sudo ./scripts/rollback_dog_jetson_route.sh
```

This is broader than the cockpit forward and should not be the first choice while debugging WebRTC.

These scripts do not persist across reboot unless the robot firmware or shell environment preserves runtime state.

## Diagnostics

On the dog:

```bash
ip addr
ip route
cat /proc/sys/net/ipv4/ip_forward
iptables -S
iptables -t nat -S
ss -lntup | grep -E '9991|9990|webrtc|unitree'
ps aux | grep -Ei 'webrtc|xfk|unitree'
curl http://127.0.0.1:9991/con_notify
```

On the Jetson:

```bash
ip addr
ip route
arp -a
ip -br link show enP8p1s0
cat /sys/class/net/enP8p1s0/carrier
ping -c 3 -W 2 10.123.0.1
ping -c 3 -W 2 192.168.123.121
ping -c 3 -W 2 8.8.8.8
nc -vz -w 3 10.123.0.1 9991
curl --max-time 5 http://10.123.0.1:9991/con_notify
pip show unitree-webrtc-connect unitree_webrtc_connect
./scripts/diagnose_webrtc.sh
```

On the WSL instance:

```bash
ping -c 3 192.168.123.121
nc -vz 192.168.123.121 9991
curl http://192.168.123.121:9991/con_notify
python -m go2_local_brain.main
```

## Recovery Order After No SDP Answer

1. Stop this app, viewers, and any phone apps.
2. Roll back dog-side NAT if it was just added.
3. Keep Jetson Ethernet on `10.123.0.2/24`; do not return it to `192.168.123.18/24` while dog eth0/wlan0 overlap.
4. Verify `curl http://192.168.123.121:9991/con_notify` from WSL.
5. Try WSL `python -m go2_local_brain.main` with `GO2_WEBRTC_METHOD=LocalSTA`.
6. If WSL still fails, restart the dog WebRTC bridge or reboot the robot.
7. Once WSL works again, test Jetson with `GO2_IP=192.168.123.121` and `GO2_WEBRTC_METHOD=LocalSTA`.

The key idea: fix WebRTC first, then expose the Jetson cockpit. Do not keep changing dog routes while debugging the SDP answer.

## Confirmed Dog Reboot Failure Mode

After a robot reboot, this firmware can restore a bad LocalSTA state:

```text
eth0 secondary address: 192.168.123.112/24
ip_forward: 1
default route via wlan0 and eth0
192.168.123.0/24 route via wlan0 and eth0
unitreeWebRTCClientMaster UDP bound to 192.168.123.161
```

In that state, `curl http://192.168.123.121:9991/con_notify` can still work, but the SDK may fail immediately after posting the SDP offer:

```text
NoSdpAnswerError: Robot signaling returned no SDP answer
RemoteDisconnected('Remote end closed connection without response')
```

One confirmed cause was the dog clock booting into the wrong year. The robot-side `/tmp/unitree_xfxton.log` showed:

```text
terminate called after throwing an instance of 'dds::core::Error'
what(): dds::core::Time::sec out of bounds
```

The recovery is:

1. Sync the dog date to the real UTC time.
2. Disable IPv4 forwarding.
3. Remove the reboot-restored eth0 `192.168.123.112` route overlap.
4. Bring eth0 down for the Wi-Fi test.
5. Restart `unitreeWebRTCClientMaster` and `xfkTon`.
6. Verify the WebRTC UDP socket binds to `192.168.123.121` or `0.0.0.0`, not `192.168.123.161`.

From the WSL instance, run the non-destructive clock/binding check:

```bash
cd ~/robotics/go2_local_brain
./scripts/recover_dog_webrtc_wifi_over_ssh.sh
```

Do not force-restart `xfkTon` or `unitreeWebRTCClientMaster` during normal startup. On this firmware that can restore video while leaving motion commands ignored. If the dog naturally binds WebRTC to `192.168.123.161`, use that as `GO2_IP`.

Then test:

```bash
GO2_AES_128_KEY= \
GO2_IP=192.168.123.161 \
GO2_WEBRTC_METHOD=LocalSTA \
VERBOSE_WEBRTC_LOGS=1 \
python -m go2_local_brain.diagnose_webrtc
```

If you are already SSHed into the dog, copy or paste `scripts/recover_dog_webrtc_wifi.sh` onto the dog and run it as root. By default it syncs/checks state only. You can also set the date explicitly:

```bash
DOG_UTC_DATE="$(date -u '+%Y-%m-%d %H:%M:%S')" ./scripts/recover_dog_webrtc_wifi.sh
```

Only use the destructive WebRTC restart path when signaling is wedged and you accept the risk that the motion stack may need a full robot reboot:

```bash
GO2_FORCE_WEBRTC_RESTART=1 ./scripts/recover_dog_webrtc_wifi.sh
```
