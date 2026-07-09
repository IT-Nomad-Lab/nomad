# nomad-voice — the local voice service

One service for all of NOMAD's voice, fully on-device (no cloud speech APIs):

- **Real-time interruptible conversation** (the ChatGPT-style headline), built on
  **[Pipecat](https://github.com/pipecat-ai/pipecat)**: audio streams both ways over **WebRTC**
  (browser echo-cancellation), **Silero VAD** yields the instant you speak (**barge-in**),
  **faster-whisper** transcribes, **NOMAD's brain** replies, **Piper** speaks it.
- **Classic endpoints** the LCARS console uses (push-to-talk, spoken replies, wake word).

```
real-time:  WebRTC in → Whisper STT → NOMAD brain (memory + intent + gate) → Piper TTS → WebRTC out
```

**Wired into NOMAD's brain.** The reply step doesn't talk to a raw model — each spoken turn is
POSTed to the console's `/api/chat` (a custom Pipecat `NomadBrainLLMService`), so the *voice*
conversation inherits everything the text console has:

- **Memory** — cross-session recall + persistence (Qdrant), same as typing.
- **Intent router / tools** — "run diagnostics", "research X", "start a project…" *do the thing*.
- **Human gate** — an external/irreversible ask ("send the email…") is **captured into the pipeline**
  and waits in the Approval Queue; say **"approve"** / **"reject"** to clear it by voice.

Markdown/emoji in the brain's replies are stripped before Piper speaks them. Set
`NOMAD_VOICE_BRAIN=0` to bypass the brain and talk straight to a LiteLLM model.

**Embedded in the console.** The LCARS console (`:1701`) has a **live-voice** button (▮▮ waveform,
next to 🎤/👂/🔊) that opens this real-time session in-page. Signaling is cross-origin to `:8200`
(CORS-allowed for localhost); WebRTC media is peer-to-peer browser↔voice. The standalone client at
**http://127.0.0.1:8200/** still works too.

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
| `NOMAD_VOICE_MODEL` | `fast` | Model alias for replies (used as the brain's `model`, or direct when brain off). |
| `NOMAD_VOICE_BRAIN` | `1` | Route replies through NOMAD's brain (memory + intent + gate). `0` = raw model. |
| `NOMAD_BRAIN_URL` | `http://127.0.0.1:1701` | The console `/api/chat` endpoint the brain call hits. |
| `NOMAD_VOICE_CORS` | `*` | Origins allowed to open the real-time client (localhost always allowed). |
| `LITELLM_BASE_URL` / `LITELLM_MASTER_KEY` | `http://127.0.0.1:4000` / — | Model gateway (brain-off path). |
| `NOMAD_STT_ENGINE` | `whisper` | Real-time STT engine: `whisper` (CPU, local) or `kyutai` (GPU sidecar [`nomad-stt`](../nomad-stt/) on :8212 — lower latency, more accurate). Same VAD seam either way. |
| `NOMAD_STT_URL` | `ws://127.0.0.1:8212/stt` | The nomad-stt sidecar WebSocket (when engine=kyutai). |
| `WHISPER_MODEL` / `WHISPER_DEVICE` | `base.en` / `cpu` | STT (whisper engine). |
| `PIPER_VOICE` | `en_GB-alan-medium` | TTS voice ("Jarvis"), in `voices/`. |
| `WAKE_MODEL` / `WAKE_THRESHOLD` | `hey_jarvis` / `0.5` | Wake word. |

## Notes

- CPU-only (Whisper/Piper/Silero/wake on CPU; the LLM runs on the gateway) — no GPU contention.
- **Phase 2 (Moshi):** the real-time STT→brain→TTS chain can be swapped for Moshi (true simultaneous
  full-duplex) inside the same Pipecat pipeline — the transport, client, and brain wiring are unchanged.
