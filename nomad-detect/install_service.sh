#!/usr/bin/env bash
# Install nomad-detect as a systemd USER service so it survives reboot (same pattern as
# nomad-stt / nomad-voice). Models load eagerly at boot so the first check is not a 15 s wait.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p "$HOME/.config/systemd/user"
cp nomad-detect.service "$HOME/.config/systemd/user/nomad-detect.service"
systemctl --user daemon-reload
systemctl --user enable --now nomad-detect
loginctl enable-linger "$USER" 2>/dev/null || true

echo "installed. status:  systemctl --user status nomad-detect"
echo "logs:               journalctl --user -u nomad-detect -f"
