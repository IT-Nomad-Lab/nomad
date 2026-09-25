#!/usr/bin/env bash
# nomad-detect install: venv + torch (Blackwell cu128) + transformers + server deps.
# The detector model pair downloads on first run (~6 GB for the 1.5B pair) into the HF cache.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip

# torch FIRST, from the CUDA 12.8 index — the wheel that ships sm_120 (Blackwell) kernels.
# Default PyPI torch does NOT run on a 5090.
./.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu128

./.venv/bin/pip install -r requirements.txt

echo
echo "done. sanity-check the GPU:"
echo "  ./.venv/bin/python -c \"import torch; print('cuda', torch.cuda.is_available())\""
echo "warm the models:   ./.venv/bin/python -c \"import binoculars; binoculars.engine()\""
echo "run it:            ./.venv/bin/python server.py"
echo "run on boot:       ./install_service.sh"
