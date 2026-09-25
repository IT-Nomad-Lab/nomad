#!/usr/bin/env bash
# Thin wrapper so /verify commands don't need to know the venv path.
# Usage: ui_check.sh <url> [ui_check.py args...]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv-playwright/bin/python3" "$DIR/ui_check.py" "$@"
