#!/usr/bin/env bash
# NOMAD — install the nomad-audio service (ACE-Step music + Stable Audio Open SFX on the RTX 5090).
# Creates a dedicated venv and installs CUDA PyTorch + ACE-Step + the HTTP service deps.
#
# ACE_STEP_VERSION picks which venv this builds (run it twice to have both):
#   1.5 (default) → a clone of ACE-Step 1.5 at tag $ACESTEP15_REF in $ACESTEP15_PROJECT_ROOT
#                   (default ~/.local/share/nomad-audio/ace-step-1.5, outside the repo), synced
#                   with uv into its .venv. Checkpoints download to its checkpoints/ on the first
#                   /music call. pip can't install 1.5: its nano-vllm dep is a path source in the
#                   clone, and it pins its own cu128 torch.
#   1             → .venv,   ACE-Step v1. Checkpoints download to ~/.cache/ace-step.
# v1 and 1.5 both install a top-level `acestep` package, so they never share a venv; run.sh
# picks the venv from the same variable. ACE-Step 1.5 is MIT, v1 Apache-2.0 (both commercial-safe).
#
# `install.sh heartmula` builds only the /song engine: HeartMuLa 3B (Apache-2.0) in its own uv venv
# under $HEARTMULA_ROOT (heartlib pins transformers/torchtune/pyarrow that clash with ACE-Step),
# heartlib at commit $HEARTMULA_REF, and about 21 GB of checkpoints in $HEARTMULA_ROOT/ckpt.
# HEARTMULA_CKPT_FROM=<dir> hard-links an existing ckpt dir (same filesystem) instead of downloading.
#
# Stable Audio Open (/sfx) weights are gated on Hugging Face and download on the first /sfx call;
# see README.md for the licence and login step.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu128}"   # cu128 = Blackwell / sm_120
ACE_STEP_VERSION="${ACE_STEP_VERSION:-1.5}"
ACESTEP15_REF="${ACESTEP15_REF:-v0.1.8}"   # tag of github.com/ace-step/ACE-Step-1.5
ACESTEP15_ROOT="${ACESTEP15_PROJECT_ROOT:-$HOME/.local/share/nomad-audio/ace-step-1.5}"
# ACE-Step 1.5 supports Python 3.11 and 3.12 only; the host python3 is newer, so pin it.
PYTHON="${PYTHON:-python3.12}"

log(){ printf '\n\033[1;35m[audio-install]\033[0m %s\n' "$*"; }

install_heartmula(){
  local root="${HEARTMULA_ROOT:-$HOME/.local/share/nomad-audio/heartmula}"
  local ref="${HEARTMULA_REF:-5858ca8}"   # github.com/HeartMuLa/heartlib, 2026-10-08
  command -v uv >/dev/null || { echo "HeartMuLa installs with uv: https://docs.astral.sh/uv/" >&2; exit 2; }
  mkdir -p "$root"
  if [ ! -d "$root/heartlib/.git" ]; then
    log "Cloning heartlib into $root/heartlib"
    git clone --quiet https://github.com/HeartMuLa/heartlib.git "$root/heartlib"
  else
    git -C "$root/heartlib" fetch --quiet origin
  fi
  git -C "$root/heartlib" checkout --quiet "$ref"
  log "uv venv $root/.venv (Python 3.10): torch 2.10 cu128 + heartlib $ref"
  [ -x "$root/.venv/bin/python" ] || uv venv -q --python 3.10 "$root/.venv"
  # pyarrow<21: torchtune 0.4.0 pulls datasets 2.14, which calls pyarrow.PyExtensionType (gone in 21).
  # soundfile: song_worker.py saves with it (torchaudio 2.10's save needs torchcodec, no FFmpeg-8 build).
  uv pip install -q --python "$root/.venv/bin/python" --index-strategy unsafe-best-match \
    --extra-index-url "$TORCH_INDEX" "torch==2.10.0" "torchaudio==2.10.0" "torchvision==0.25.0" \
    -e "$root/heartlib" "pyarrow<21" "soundfile>=0.12"
  local ck="$root/ckpt"
  if [ -n "${HEARTMULA_CKPT_FROM:-}" ] && [ ! -e "$ck" ]; then
    log "Hard-linking checkpoints from $HEARTMULA_CKPT_FROM"
    cp -al "$HEARTMULA_CKPT_FROM" "$ck"
  fi
  local hf="$root/.venv/bin/hf"
  [ -e "$ck/tokenizer.json" ]                   || "$hf" download --local-dir "$ck" HeartMuLa/HeartMuLaGen
  [ -e "$ck/HeartMuLa-oss-3B/config.json" ]     || "$hf" download --local-dir "$ck/HeartMuLa-oss-3B" HeartMuLa/HeartMuLa-oss-3B-happy-new-year
  [ -e "$ck/HeartCodec-oss/config.json" ]       || "$hf" download --local-dir "$ck/HeartCodec-oss" HeartMuLa/HeartCodec-oss-20260123
  "$root/.venv/bin/python" -I -c "import heartlib, torch; print('heartlib ok, torch', torch.__version__)"
  log "Done. Restart nomad-audio; /health shows song.installed. Run /song under gpu_guard."
}

if [ "${1:-}" = heartmula ]; then install_heartmula; exit 0; fi

case "$ACE_STEP_VERSION" in
  1|v1|1.0)
    VENV="${NOMAD_AUDIO_VENV_V1:-$HERE/.venv}"
    ACESTEP_REPO="${ACESTEP_REPO:-git+https://github.com/ace-step/ACE-Step.git}"
    log "Creating venv at $VENV ($("$PYTHON" --version)) for ACE-Step v1"
    "$PYTHON" -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip wheel
    log "Installing CUDA PyTorch (cu128)"
    # torchvision MUST come from the same cu128 index — transformers (ACE-Step dep) imports it, and a
    # CPU-build torchvision against cu128 torch fails with "operator torchvision::nms does not exist".
    "$VENV/bin/pip" install --index-url "$TORCH_INDEX" torch torchvision torchaudio
    log "Installing ACE-Step ($ACESTEP_REPO)"
    "$VENV/bin/pip" install "$ACESTEP_REPO"
    log "Installing HTTP service + Stable Audio Open deps"
    "$VENV/bin/pip" install -r "$HERE/requirements.txt" ;;
  1.5|v1.5|15)
    VENV="${NOMAD_AUDIO_VENV_V15:-$ACESTEP15_ROOT/.venv}"
    command -v uv >/dev/null || { echo "ACE-Step 1.5 installs with uv: https://docs.astral.sh/uv/" >&2; exit 2; }
    if [ ! -d "$ACESTEP15_ROOT/.git" ]; then
      log "Cloning ACE-Step 1.5 $ACESTEP15_REF into $ACESTEP15_ROOT"
      mkdir -p "$(dirname "$ACESTEP15_ROOT")"
      git clone --quiet --branch "$ACESTEP15_REF" --depth 1 \
        https://github.com/ace-step/ACE-Step-1.5.git "$ACESTEP15_ROOT"
    else
      log "Checking out $ACESTEP15_REF in $ACESTEP15_ROOT"
      git -C "$ACESTEP15_ROOT" fetch --quiet --depth 1 origin tag "$ACESTEP15_REF"
      git -C "$ACESTEP15_ROOT" checkout --quiet "$ACESTEP15_REF"
    fi
    log "uv sync into $VENV (upstream's install: its pinned cu128 torch + nano-vllm)"
    (cd "$ACESTEP15_ROOT" && UV_PROJECT_ENVIRONMENT="$VENV" uv sync --python "$PYTHON")
    log "Installing HTTP service + Stable Audio Open deps"
    uv pip install --python "$VENV/bin/python" -r "$HERE/requirements.txt" ;;
  *) echo "ACE_STEP_VERSION=$ACE_STEP_VERSION, use 1.5 or 1" >&2; exit 2 ;;
esac

log "Done."
log "Start manually:  ACE_STEP_VERSION=$ACE_STEP_VERSION $HERE/run.sh    (listens on :8220)"
log "Or run on boot:  $HERE/install_service.sh"
log "The first /music request downloads the ACE-Step checkpoints; the first /sfx downloads SAO."
