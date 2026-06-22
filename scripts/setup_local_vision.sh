#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .venv/bin/activate ]]; then
  echo "Missing .venv. Create the WSL project environment first." >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Install CPU PyTorch explicitly. The generic Linux wheel can pull several
# gigabytes of CUDA libraries that this host cockpit does not need.
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
python -m pip install insightface onnxruntime ultralytics

echo "Local Buffalo, YOLO face, and YOLO person runtime is ready."
echo "Models are checksum-verified and cached by ./scripts/run_local_cockpit.sh."
