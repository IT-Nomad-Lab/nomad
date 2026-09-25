#!/usr/bin/env bash
# Install nomad-social as a systemd USER service (same pattern as nomad-detect).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p "$HOME/.config/systemd/user"
cp nomad-social.service "$HOME/.config/systemd/user/nomad-social.service"
systemctl --user daemon-reload
systemctl --user enable --now nomad-social
loginctl enable-linger "$USER" 2>/dev/null || true
echo "installed. status:  systemctl --user status nomad-social"
echo "logs:               journalctl --user -u nomad-social -f"
