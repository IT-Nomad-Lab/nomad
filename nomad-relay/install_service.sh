#!/usr/bin/env bash
# venv → tests → Zulip bootstrap (channels, push settings, org hardening) → systemd user service.
set -euo pipefail
cd "$(dirname "$0")"

# zulip-admin.zuliprc is only needed for bootstrap, which deletes it when done.
[ -f ~/.config/nomad/zulip-bot.zuliprc ] || { echo "✗ missing ~/.config/nomad/zulip-bot.zuliprc (see zulip/setup.sh output)"; exit 1; }
chmod 600 ~/.config/nomad/zulip-*.zuliprc

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt pytest
.venv/bin/python -m pytest -q tests

if [ -f ~/.config/nomad/zulip-admin.zuliprc ]; then
  .venv/bin/python bootstrap.py
elif [ ! -f ~/.config/nomad/relay.env ]; then
  echo "✗ first install needs ~/.config/nomad/zulip-admin.zuliprc for bootstrap"; exit 1
fi

mkdir -p ~/.config/systemd/user
cp nomad-relay.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nomad-relay
sleep 3
systemctl --user --no-pager status nomad-relay | head -12
