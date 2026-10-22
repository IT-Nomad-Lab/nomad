#!/usr/bin/env bash
# NOMAD — start nomad-audio in the venv that matches ACE_STEP_VERSION.
# ACE-Step v1 and 1.5 both install a top-level `acestep` package, so they cannot share a venv:
# v1 lives in .venv, 1.5 in its uv clone's .venv (see install.sh). Flipping ACE_STEP_VERSION and restarting is the rollback.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export ACE_STEP_VERSION="${ACE_STEP_VERSION:-1.5}"

case "$ACE_STEP_VERSION" in
  1|v1|1.0)    VENV="${NOMAD_AUDIO_VENV_V1:-$HERE/.venv}" ;;
  1.5|v1.5|15) VENV="${NOMAD_AUDIO_VENV_V15:-${ACESTEP15_PROJECT_ROOT:-$HOME/.local/share/nomad-audio/ace-step-1.5}/.venv}" ;;
  *) echo "nomad-audio: ACE_STEP_VERSION=$ACE_STEP_VERSION, use 1.5 or 1" >&2; exit 2 ;;
esac

# Refuse rather than fall back: a silent v1 start under ACE_STEP_VERSION=1.5 would look upgraded.
if [ ! -x "$VENV/bin/python" ]; then
  echo "nomad-audio: no venv at $VENV for ACE-Step $ACE_STEP_VERSION;" \
       "run: ACE_STEP_VERSION=$ACE_STEP_VERSION $HERE/install.sh" >&2
  exit 3
fi
exec "$VENV/bin/python" "$HERE/server.py"
