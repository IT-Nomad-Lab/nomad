#!/usr/bin/env bash
# NOMAD — add Qwen-Image to ComfyUI (best open prompt-adherence + text rendering; Apache-2.0).
# Qwen ships as SPLIT components (DiT + Qwen2.5-VL text encoder + VAE), placed in their own dirs.
# Files from Comfy-Org's packaging (fp8, ComfyUI-native, no custom nodes needed).
set -euo pipefail
COMFY_DIR="${COMFYUI_DIR:-$HOME/ComfyUI}"
BASE="https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files"

log(){ printf '\n\033[1;36m[qwen-install]\033[0m %s\n' "$*"; }
fetch(){ # fetch <url> <dest>  (resumable; skips if already a sane size)
  local url="$1" dest="$2" min="${3:-200000000}"
  mkdir -p "$(dirname "$dest")"
  if [ -f "$dest" ] && [ "$(stat -c%s "$dest" 2>/dev/null || echo 0)" -gt "$min" ]; then
    log "Have $(basename "$dest") ($(du -h "$dest" | cut -f1)) — skip"; return 0; fi
  log "Downloading $(basename "$dest")"
  curl -L --fail --retry 5 --retry-delay 5 -C - -o "$dest" "$url"
}

fetch "$BASE/diffusion_models/qwen_image_fp8_e4m3fn.safetensors" \
      "$COMFY_DIR/models/diffusion_models/qwen_image_fp8_e4m3fn.safetensors" 5000000000
fetch "$BASE/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors" \
      "$COMFY_DIR/models/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors" 3000000000
fetch "$BASE/vae/qwen_image_vae.safetensors" \
      "$COMFY_DIR/models/vae/qwen_image_vae.safetensors" 100000000

log "Done. Qwen-Image components:"
ls -lh "$COMFY_DIR/models/diffusion_models/qwen_image_fp8_e4m3fn.safetensors" \
       "$COMFY_DIR/models/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors" \
       "$COMFY_DIR/models/vae/qwen_image_vae.safetensors" 2>/dev/null || true
log "Set COMFYUI_MODEL=qwen to use it."
