#!/usr/bin/env bash
# nomad-stt install: venv + torch (Blackwell cu128) + Kyutai STT (moshi) + server deps.
# The Kyutai model downloads on first run (token-less, CC-BY) into the HF cache.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip

# torch FIRST, from the CUDA 12.8 index — this is the wheel that ships sm_120 (Blackwell) kernels.
# Every other GPU dep on this box needs this; the default PyPI torch does NOT run on a 5090.
./.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu128

# Kyutai STT (PyTorch) + the HTTP/WebSocket server
./.venv/bin/pip install -r requirements.txt

echo
echo "done. sanity-check the GPU:"
echo "  ./.venv/bin/python -c \"import torch; print('cuda', torch.cuda.is_available())\""
echo "run it:            ./.venv/bin/python server.py"
echo "run on boot:       ./install_service.sh"
