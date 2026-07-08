#!/usr/bin/env bash
# NOMAD — install the nomad-audio service (ACE-Step music/sound generation on the RTX 5090).
# Creates a dedicated venv and installs CUDA PyTorch + ACE-Step + the HTTP service deps.
# Model checkpoints auto-download to ~/.cache/ace-step on the first /music call (~few GB).
# ACE-Step is Apache-2.0 (commercial-safe).
#
# To try ACE-Step 1.5 instead, set before running:
#   ACESTEP_REPO=git+https://github.com/ace-step/ACE-Step-1.5.git
# (and set ACESTEP_CHECKPOINT in the service env if 1.5 needs an explicit checkpoint path).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${NOMAD_AUDIO_VENV:-$HERE/.venv}"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu128}"   # cu128 = Blackwell / sm_120
ACESTEP_REPO="${ACESTEP_REPO:-git+https://github.com/ace-step/ACE-Step.git}"

log(){ printf '\n\033[1;35m[audio-install]\033[0m %s\n' "$*"; }

log "Creating venv at $VENV ($(python3 --version))"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel

log "Installing CUDA PyTorch (cu128)"
# torchvision MUST come from the same cu128 index — transformers (ACE-Step dep) imports it, and a
# CPU-build torchvision against cu128 torch fails with "operator torchvision::nms does not exist".
"$VENV/bin/pip" install --index-url "$TORCH_INDEX" torch torchvision torchaudio

log "Installing ACE-Step ($ACESTEP_REPO)"
"$VENV/bin/pip" install "$ACESTEP_REPO"

log "Installing HTTP service deps"
"$VENV/bin/pip" install -r "$HERE/requirements.txt"

log "Done."
log "Start manually:  $VENV/bin/python $HERE/server.py    (listens on :8220)"
log "Or run on boot:  $HERE/install_service.sh"
log "The first /music request downloads the ACE-Step checkpoints to ~/.cache/ace-step."
