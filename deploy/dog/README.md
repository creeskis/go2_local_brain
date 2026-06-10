# Dog-side deployment

The dog runs **one** systemd unit: `jetson-bridge.service`. It enables
IP forwarding + a source-restricted NAT so the Jetson (on `10.42.0.0/24`)
can reach the internet through the dog's WiFi uplink.

## Install (on the dog, as root)

```bash
# From your laptop, after cloning this repo:
scp deploy/dog/jetson-bridge.service root@192.168.123.121:/etc/systemd/system/

ssh root@192.168.123.121
systemctl daemon-reload
systemctl enable --now jetson-bridge
systemctl status jetson-bridge --no-pager
```

## Verify (from the dog)

```bash
ip -br addr | grep eth0           # expect 10.42.0.1/24 added alongside existing
iptables -t nat -S | grep 10.42   # expect MASQUERADE rule
iptables -S | grep 10.42          # expect two FORWARD rules
cat /proc/sys/net/ipv4/ip_forward  # expect 1
```

## Uninstall

```bash
systemctl disable --now jetson-bridge
rm /etc/systemd/system/jetson-bridge.service
systemctl daemon-reload
reboot                             # cleanest way to flush leftover state
```

## What this WON'T do

The unit deliberately does **not**:

- Touch `eth0`'s existing addresses (`192.168.123.161/24`, `192.168.123.112/24`).
- Touch `wlan0` at all.
- Modify the dog's default routes.
- MASQUERADE any traffic except `10.42.0.0/24`.

If you previously ran free-form iptables surgery and the dog's WebRTC
broke, those changes are NOT in this unit — the unit's `ExecStop` only
undoes what the `ExecStart` set up. If 9991 still isn't responsive after
enabling the unit, the problem is elsewhere (the WebRTC daemons
themselves); see `docs/recover_webrtc.md` (TODO) or restart manually:

```bash
pkill -f unitreeWebRTCClientMaster ; pkill -f xfkTon ; sleep 1
nohup /unitree/module/webrtc_bridge/bin/unitreeWebRTCClientMaster \
    --enable_multi_session true >/tmp/unitree_webrtc_master.log 2>&1 &
sleep 3
nohup /unitree/module/webrtc_bridge/src/webrtc_dds_bridge/xfkTon \
    >/tmp/unitree_xfxton.log 2>&1 &
sleep 5
ss -lntup | grep -E '9991|webrtc|unitree'
```
