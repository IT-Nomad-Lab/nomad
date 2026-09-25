#!/usr/bin/env bash
# nomad-social install: venv + deps. The Whisper model (small, ~480 MB) downloads on first use.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
echo "run it:       ./.venv/bin/python server.py      (or: cli.py links.txt)"
echo "run on boot:  ./install_service.sh"
