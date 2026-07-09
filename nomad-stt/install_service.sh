#!/usr/bin/env bash
# Run nomad-stt on boot as a systemd USER service (same pattern as nomad-voice).
set -euo pipefail
cd "$(dirname "$0")"

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp nomad-stt.service "$UNIT_DIR/nomad-stt.service"

systemctl --user daemon-reload
systemctl --user enable --now nomad-stt.service
loginctl enable-linger "$USER" 2>/dev/null || true

echo "nomad-stt enabled + started. manage with:"
echo "  systemctl --user status nomad-stt"
echo "  systemctl --user restart nomad-stt"
echo "  journalctl --user -u nomad-stt -f"
