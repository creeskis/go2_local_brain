# Jetson Orin Nano deployment

One-shot install that turns a fresh Jetson into a robot brain appliance:

- Networking: Jetson sits on `10.42.0.2`, dog acts as NAT router on
  `10.42.0.1`. See `deploy/dog/jetson-bridge.service` for the
  dog-side counterpart (you install that one ON THE DOG).
- Ollama auto-starts and pulls the configured model.
- `ai_autonomy_gui` auto-starts on port 8775 bound to all interfaces
  with bearer-token auth.
- Optional: `viewer` auto-starts on port 8765 for a read-only feed.

## Pre-flight (on the dog) — do this FIRST

Copy `deploy/dog/jetson-bridge.service` to the dog and enable it:

```bash
scp deploy/dog/jetson-bridge.service root@192.168.123.121:/etc/systemd/system/
ssh root@192.168.123.121 'systemctl daemon-reload && systemctl enable --now jetson-bridge && systemctl status jetson-bridge --no-pager'
```

Verify from the Jetson side (still on `192.168.123.18` for now):

```bash
ping -c 3 1.1.1.1                        # internet via dog WiFi
ping -c 3 192.168.123.121                # dog reachable through bridge
```

Only proceed below once both work.

## Install (on the Jetson)

```bash
cd ~
mkdir -p robotics && cd robotics
git clone https://github.com/creeskis/go2_local_brain.git
cd go2_local_brain

# One-shot install. Runs sudo for apt/systemd; uses your home dir for the venv.
sudo bash deploy/jetson/install.sh
```

That will:

1. apt-install Python 3, venv, git, curl, portaudio19-dev, ffmpeg.
2. Switch the Jetson's wired connection to `10.42.0.2/24` via NetworkManager.
3. Create `~/.go2/venv` and `pip install -e .` the repo.
4. Install Ollama (`https://ollama.com/install.sh`) and pull the model
   listed in `.env` (default `gemma3:4b`).
5. Write a fresh `.env` from `.env.example` with sane defaults.
6. Install three systemd units: `go2-bridge-link.service`,
   `go2-ollama.service`, `go2-brain.service`. Enable + start them all.

After install you should see:

```bash
systemctl status go2-brain --no-pager
curl http://localhost:8775/ -o /dev/null -w '%{http_code}\n'   # 401 (auth)
journalctl -u go2-brain -f                                       # follow live
```

Grab the auth URL the brain printed on first start:

```bash
journalctl -u go2-brain --no-pager | grep -E "auth token|Browser URL"
```

Open that URL from your laptop browser. You're done.

## Bumping the model later

```bash
echo 'OLLAMA_MODEL=qwen3:8b' >> ~/.go2/env.local        # overrides .env
sudo systemctl restart go2-ollama go2-brain
```

`go2-ollama` will `ollama pull qwen3:8b` automatically before `go2-brain`
tries to connect.

## Uninstall

```bash
sudo bash deploy/jetson/uninstall.sh
```

Removes the systemd units, leaves the repo + venv on disk.
