#!/usr/bin/env bash
# Install + enable nomad-voice as a systemd user service (loads LITELLM_MASTER_KEY from ~/nomad/.env).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp "$HERE/nomad-voice.service" "$UNIT_DIR/nomad-voice.service"
systemctl --user daemon-reload
systemctl --user enable --now nomad-voice.service
loginctl enable-linger "$USER" 2>/dev/null || true
echo "nomad-voice enabled. Open http://127.0.0.1:8200/  ·  status: systemctl --user status nomad-voice"
