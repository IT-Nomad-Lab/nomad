# nomad-voice — the local voice service

One service for all of NOMAD's voice, fully on-device (no cloud speech APIs):

- **Real-time interruptible conversation** (the ChatGPT-style headline), built on
  **[Pipecat](https://github.com/pipecat-ai/pipecat)**: audio streams both ways over **WebRTC**
  (browser echo-cancellation), **Silero VAD** yields the instant you speak (**barge-in**),
  **faster-whisper** transcribes, a **LiteLLM** model replies (streamed), **Piper** speaks it.
- **Classic endpoints** the LCARS console uses (push-to-talk, spoken replies, wake word).

```
real-time:  WebRTC in → Whisper STT → LLM (LiteLLM) → Piper TTS → WebRTC out   (interruptions on)
```

| Endpoint | Body / proto | Returns |
|---|---|---|
| `GET  /` | browser | the real-time WebRTC voice client (open http://127.0.0.1:8200/) |
| `POST /api/offer` | WebRTC SDP | real-time signaling |
| `POST /tts` | `{text}` | `audio/wav` (Piper) |
| `POST /stt` | `file=<audio>` | `{text}` (faster-whisper) |
| `WS   /wake` | 16 kHz int16 PCM | `{wake, score}` (openWakeWord "hey jarvis") |
| `GET  /health` | — | status |

**Host-native** (the real-time WebRTC needs direct networking) — not a container. The engine and
console reach it at `host.docker.internal:8200`.

## Run it

```bash
nomad-voice/install.sh            # venv + Pipecat (whisper/silero/webrtc/openai/piper) + whisper/piper/wake models
nomad-voice/install_service.sh    # run on boot (systemd user unit; loads LITELLM_MASTER_KEY from ~/nomad/.env)
```

Then open **http://127.0.0.1:8200/** — grant the mic and talk; interrupt any time.

System deps: **espeak-ng** (Piper) + **ffmpeg** (Whisper audio decode) — install via your package
manager if `/tts` or `/stt` error.

## Key environment variables

| Var | Default | Meaning |
|---|---|---|
| `NOMAD_VOICE_PORT` | `8200` | HTTP/WebRTC port. |
| `NOMAD_VOICE_MODEL` | `fast` | LiteLLM role alias for real-time replies (`fast` = lowest latency). |
| `LITELLM_BASE_URL` / `LITELLM_MASTER_KEY` | `http://127.0.0.1:4000` / — | Model gateway. |
| `WHISPER_MODEL` / `WHISPER_DEVICE` | `base.en` / `cpu` | STT. |
| `PIPER_VOICE` | `en_GB-alan-medium` | TTS voice ("Jarvis"), in `voices/`. |
| `WAKE_MODEL` / `WAKE_THRESHOLD` | `hey_jarvis` / `0.5` | Wake word. |

## Notes

- CPU-only (Whisper/Piper/Silero/wake on CPU; the LLM runs on the gateway) — no GPU contention.
- **Phase 2 (Moshi):** the real-time STT→LLM→TTS chain can be swapped for Moshi (true simultaneous
  full-duplex) inside the same Pipecat pipeline — the transport, client, and wiring are unchanged.
- Not yet wired to the engine's memory/tools/gate, and not embedded in the console UI — next steps.
