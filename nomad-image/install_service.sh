#!/usr/bin/env bash
# Install + enable the ComfyUI boot service (systemd user unit, like nomad-dispatch/nomad-term).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp "$HERE/nomad-comfyui.service" "$UNIT_DIR/nomad-comfyui.service"
systemctl --user daemon-reload
systemctl --user enable --now nomad-comfyui.service
loginctl enable-linger "$USER" 2>/dev/null || true   # survive logout/boot
echo "nomad-comfyui enabled. Status:  systemctl --user status nomad-comfyui"
