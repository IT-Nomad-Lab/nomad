#!/usr/bin/env bash
# NOMAD — add FLUX.2 [dev] to ComfyUI (best local quality + prompt adherence; closest local
# step toward gpt-image-1). Split components: DiT + Mistral-3 text encoder + VAE (fp8-mixed,
# ComfyUI-native, no custom nodes). Fits a 24GB card in mixed precision; comfortable on 32GB.
# ⚠ LICENSE: FLUX [dev] is a NON-COMMERCIAL community license. For assets you intend to SELL
#   (e.g. Income Lab / Etsy), use Qwen-Image (Apache-2.0) instead — install_qwen.sh.
set -euo pipefail
COMFY_DIR="${COMFYUI_DIR:-$HOME/ComfyUI}"
BASE="https://huggingface.co/Comfy-Org/flux2-dev/resolve/main/split_files"

log(){ printf '\n\033[1;36m[flux2-install]\033[0m %s\n' "$*"; }
fetch(){ # fetch <url> <dest> <min-bytes>  (resumable; skips if already a sane size)
  local url="$1" dest="$2" min="${3:-200000000}"
  mkdir -p "$(dirname "$dest")"
  if [ -f "$dest" ] && [ "$(stat -c%s "$dest" 2>/dev/null || echo 0)" -gt "$min" ]; then
    log "Have $(basename "$dest") ($(du -h "$dest" | cut -f1)) — skip"; return 0; fi
  log "Downloading $(basename "$dest")"
  curl -L --fail --retry 5 --retry-delay 5 -C - -o "$dest" "$url"
}

fetch "$BASE/diffusion_models/flux2_dev_fp8mixed.safetensors" \
      "$COMFY_DIR/models/diffusion_models/flux2_dev_fp8mixed.safetensors" 20000000000
fetch "$BASE/text_encoders/mistral_3_small_flux2_bf16.safetensors" \
      "$COMFY_DIR/models/text_encoders/mistral_3_small_flux2_bf16.safetensors" 3000000000
fetch "$BASE/vae/flux2-vae.safetensors" \
      "$COMFY_DIR/models/vae/flux2-vae.safetensors" 50000000

log "Done. FLUX.2 [dev] components:"
ls -lh "$COMFY_DIR/models/diffusion_models/flux2_dev_fp8mixed.safetensors" \
       "$COMFY_DIR/models/text_encoders/mistral_3_small_flux2_bf16.safetensors" \
       "$COMFY_DIR/models/vae/flux2-vae.safetensors" 2>/dev/null || true
log "Set COMFYUI_MODEL=flux2 to use it. Needs a recent ComfyUI (native FLUX.2 nodes)."
