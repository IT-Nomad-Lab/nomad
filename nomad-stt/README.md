# nomad-stt — Kyutai streaming STT on the GPU

The low-latency ear for NOMAD's voice. A GPU sidecar that runs a **Kyutai** speech-to-text model
(delayed-streams-modeling, PyTorch) and streams transcription over a WebSocket: send it 24 kHz mono
PCM, get text pieces back the instant they emit. It replaces CPU Whisper in the voice pipeline. The
LLM brain and Piper TTS in [`nomad-voice`](../nomad-voice/) stay exactly as they are.

```
mic → (nomad-voice) → 24kHz PCM over WS → nomad-stt (Kyutai on GPU) → text pieces → brain → Piper
```

## Why it exists

Whisper on this box runs on CPU and works in chunks: record a slice, then transcribe it. That adds
latency and makes barge-in clumsy. Kyutai STT streams. Text lands about half a second behind your
voice, so the assistant can react while you are still talking.

Measured on the RTX 5090 (Blackwell), warm:

- **18 ms per 80 ms frame** — 4.36x real-time on a single stream.
- **~1.0 s** from speaking a word to its text arriving over the socket (0.5 s model delay + streaming).

Blackwell was the risk. It cleared: `torch` from the CUDA 12.8 wheel ships `sm_120` kernels, the
model loads and runs, token-less (the Kyutai models are CC-BY, no Hugging Face account needed).

## API

Host-native on `127.0.0.1:8212`.

| Endpoint | Proto | Notes |
|---|---|---|
| `WS /stt` | binary in = 24 kHz mono **int16** PCM (any chunk size) | text pieces stream back as `{"text": "…"}` |
| | text in = `{"type":"flush"}` | drain trailing text (call when the speaker stops) → `{"type":"flushed"}` |
| | text in = `{"type":"reset"}` | start a new utterance (clears streaming state) |
| `GET /health` | — | model / device / sample rate |

One active stream at a time (batch 1 — one speaker). A new connection resets the state.

## Run it

```bash
./install.sh            # venv + torch (cu128/Blackwell) + moshi + server deps
./.venv/bin/python server.py     # foreground; model downloads on first run (~2-3 GB, token-less)
./install_service.sh    # or: run on boot (systemd user unit, port 8212)
```

The service **warms up at startup** (feeds silence through the model) so the first real utterance
pays no cold-kernel cost. Cold, the first frames run at ~136 ms; warm, ~18 ms.

## Environment

| Var | Default | Meaning |
|---|---|---|
| `KYUTAI_STT_REPO` | `kyutai/stt-1b-en_fr` | STT model. The 1B en/fr has ~0.5 s delay (best for real-time). `kyutai/stt-2.6b-en` is more accurate but ~2.5 s delay. |
| `KYUTAI_DEVICE` | `cuda` | `cpu` works but is not real-time. |
| `NOMAD_STT_PORT` | `8212` | 8200 voice · 8210 scraper · 8212 stt. |

## Notes

- CPU-free on the voice side: the model runs here on the GPU; `nomad-voice` stays a thin client.
- Contends with ComfyUI/FLUX2 for VRAM (the 1B model is ~2-3 GB, so there is headroom on 24 GB).
- Phase 2b: Kyutai **TTS** can join this same sidecar to replace Piper for a fully-Kyutai speech
  layer. The model loader and streaming pattern are the same.
