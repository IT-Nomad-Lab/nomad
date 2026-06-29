#!/usr/bin/env bash
# NOMAD — install ComfyUI for LOCAL image generation on the RTX 5090 (Blackwell / sm_120).
#
# Installs ComfyUI in WSL with its own venv + PyTorch built for CUDA 12.8 (cu128 — required for
# Blackwell), then downloads the SDXL base and FLUX.1 schnell (fp8) checkpoints. ComfyUI then serves
# an HTTP API on 0.0.0.0:8188 so the v2 engine container can reach it via host.docker.internal:8188.
#
# Idempotent: re-running skips what's already present and resumes partial model downloads.
# Heavy: PyTorch (~3 GB) + SDXL (~6.5 GB) + FLUX schnell fp8 (~17 GB). Expect a long first run.
set -euo pipefail

COMFY_DIR="${COMFYUI_DIR:-$HOME/ComfyUI}"
PY="${PYTHON:-python3}"
TORCH_INDEX="https://download.pytorch.org/whl/cu128"   # Blackwell needs cu128

log() { printf '\n\033[1;36m[comfyui-install]\033[0m %s\n' "$*"; }

# 1) Clone (or update) ComfyUI ------------------------------------------------------------
if [ ! -d "$COMFY_DIR/.git" ]; then
  log "Cloning ComfyUI → $COMFY_DIR"
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git "$COMFY_DIR"
else
  log "ComfyUI already present at $COMFY_DIR (skipping clone)"
fi
cd "$COMFY_DIR"

# 2) venv + dependencies ------------------------------------------------------------------
if [ ! -d "$COMFY_DIR/venv" ]; then
  log "Creating venv"
  "$PY" -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip wheel

if ! python -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
  log "Installing PyTorch (cu128 for Blackwell) — this is the big one"
  pip install --index-url "$TORCH_INDEX" torch torchvision torchaudio
fi
log "Installing ComfyUI requirements"
pip install -r requirements.txt

# 3) Model checkpoints --------------------------------------------------------------------
CKPT_DIR="$COMFY_DIR/models/checkpoints"
mkdir -p "$CKPT_DIR"

fetch() {  # fetch <url> <dest>  (resumable; skips if already a sane size)
  local url="$1" dest="$2"
  if [ -f "$dest" ] && [ "$(stat -c%s "$dest" 2>/dev/null || echo 0)" -gt 1000000000 ]; then
    log "Already have $(basename "$dest") ($(du -h "$dest" | cut -f1)) — skipping"
    return 0
  fi
  log "Downloading $(basename "$dest")"
  curl -L --fail --retry 5 --retry-delay 5 -C - -o "$dest" "$url"
}

# SDXL base 1.0 (public, ~6.5 GB)
fetch "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors" \
      "$CKPT_DIR/sd_xl_base_1.0.safetensors"

# FLUX.1 schnell, fp8 all-in-one checkpoint (Apache-2.0, public, ~17 GB) — loads via CheckpointLoaderSimple
fetch "https://huggingface.co/Comfy-Org/flux1-schnell/resolve/main/flux1-schnell-fp8.safetensors" \
      "$CKPT_DIR/flux1-schnell-fp8.safetensors"

log "Done. Checkpoints in $CKPT_DIR:"
ls -lh "$CKPT_DIR" || true
log "Start ComfyUI with:  $COMFY_DIR/venv/bin/python $COMFY_DIR/main.py --listen 0.0.0.0 --port 8188"
log "Or enable the boot service:  nomad-image/install_service.sh"
