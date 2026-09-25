#!/usr/bin/env bash
# Install the Codex bridge as a systemd USER service so it survives a reboot,
# matching how nomad-claude-bridge is run.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p "$HOME/.config/systemd/user"
cp nomad-codex-bridge.service "$HOME/.config/systemd/user/nomad-codex-bridge.service"
systemctl --user daemon-reload
systemctl --user enable --now nomad-codex-bridge
loginctl enable-linger "$USER" >/dev/null 2>&1 || true
sleep 2
systemctl --user --no-pager status nomad-codex-bridge | head -12
echo
echo "Health:  curl -s localhost:8089/health"
