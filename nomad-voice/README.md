# nomad-voice — on-device speech (TTS + STT)

Keeps NOMAD's voice **fully on-device** — no cloud speech APIs. A small FastAPI service that the
[`nomad-console/`](../nomad-console/) proxies; the browser only ever talks to the console, so
audio never leaves the machine.

| Endpoint | In → Out | Engine |
|---|---|---|
| `POST /tts` | `{text}` → `audio/wav` | **Piper** neural TTS |
| `POST /stt` | `file=<audio blob>` → `{text}` | **faster-whisper** transcription |
| `GET /health` | — | Liveness |

- TTS default voice is `en_GB-alan-medium` (calm British male); swap via `PIPER_MODEL`.
- STT runs on **CPU int8** by default (reliable on the Blackwell GPU, which still needs
  CTranslate2/cuDNN compat shaken out). Set `WHISPER_DEVICE=cuda` to try the GPU.
- An `initial_prompt` vocab hint keeps "NOMAD" from being transcribed as "No made".

The models are baked into the image. The service binds to localhost (host-only :8200).

## Run it

```bash
docker compose up -d nomad-voice     # host-only :8200
```

## Key environment variables

| Var | Default | Meaning |
|---|---|---|
| `PIPER_MODEL` | `/models/en_GB-alan-medium.onnx` | TTS voice model. |
| `WHISPER_MODEL` | `base.en` | STT model. |
| `WHISPER_DEVICE` | `cpu` | `cpu` (int8) or `cuda` (float16). |
