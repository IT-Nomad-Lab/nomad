#!/usr/bin/env bash
# Install + enable the nomad-audio boot service (systemd user unit, like nomad-comfyui).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp "$HERE/nomad-audio.service" "$UNIT_DIR/nomad-audio.service"
systemctl --user daemon-reload
systemctl --user enable --now nomad-audio.service
loginctl enable-linger "$USER" 2>/dev/null || true   # survive logout/boot
echo "nomad-audio enabled. Status:  systemctl --user status nomad-audio"
