#!/usr/bin/env bash
# NOMAD — install nomad-voice (the single voice service): real-time interruptible conversation
# (Pipecat: Silero VAD + faster-whisper + LiteLLM + Piper over WebRTC) PLUS the classic
# /tts /stt /wake endpoints the console uses. All LOCAL, CPU-friendly.
# System deps: espeak-ng (Piper phonemization) + ffmpeg (Whisper audio decode) — install with your
# package manager if `piper`/`/stt` error (e.g. sudo apt install espeak-ng ffmpeg).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${NOMAD_VOICE_VENV:-$HERE/.venv}"

log(){ printf '\n\033[1;36m[voice-install]\033[0m %s\n' "$*"; }

log "Creating venv at $VENV ($(python3 --version))"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel

log "Installing Pipecat + extras + server + wake word"
"$VENV/bin/pip" install "pipecat-ai[whisper,silero-vad,small-webrtc,webrtc,openai,piper]"
"$VENV/bin/pip" install "uvicorn[standard]" fastapi openwakeword onnxruntime
# kokoro-onnx: the smooth/natural TTS engine (NOMAD_TTS_ENGINE=kokoro); model auto-downloads on first use
"$VENV/bin/pip" install kokoro-onnx

log "Baking the Whisper STT model (base.en, CPU int8)"
"$VENV/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8')"
# openWakeWord 0.4 ships the 'hey_jarvis' model in its package resources — nothing to download.

log "Downloading the Piper voice (en_GB-alan-medium — NOMAD's 'Jarvis') → voices/"
VOICES="$HERE/voices"; mkdir -p "$VOICES"
base=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium
for f in en_GB-alan-medium.onnx en_GB-alan-medium.onnx.json; do
  [ -f "$VOICES/$f" ] || curl -fsSL -o "$VOICES/$f" "$base/$f"
done

log "Done. Start:  $VENV/bin/python $HERE/server.py   (open http://127.0.0.1:8200/)"
log "Or run on boot:  $HERE/install_service.sh"
