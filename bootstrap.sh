#!/usr/bin/env bash
# One-shot setup for a fresh WSL Linux box.
# Run from inside the project root:  bash bootstrap.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> apt prerequisites (will sudo)"
sudo apt-get update
# portaudio19-dev is required because unitree_webrtc_connect pulls in
# pyaudio, which builds from source on Linux.
sudo apt-get install -y python3 python3-venv python3-pip git portaudio19-dev

echo "==> venv"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> pip"
pip install --upgrade pip
pip install -e .

echo "==> .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "    Wrote .env from .env.example -- edit GO2_IP / GO2_AES_128_KEY before running."
fi

echo "==> smoke test"
python scripts/smoke_test_imports.py

cat <<EOF

Done. Next steps:
  1. Edit .env  (GO2_IP, optional GO2_AES_128_KEY, OLLAMA_MODEL)
  2. Make sure Ollama is reachable:    ollama list
     and the model is pulled:          ollama pull "\${OLLAMA_MODEL:-qwen3:1.7b}"
  3. Activate the venv:                source .venv/bin/activate
  4. Run the brain:                    python -m go2_local_brain.main
EOF
