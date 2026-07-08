# nomad-audio — local music & sound generation

On-device music/sound generation for NOMAD, powered by **[ACE-Step](https://github.com/ace-step/ACE-Step)**
(Apache-2.0 — **commercial-safe**). Completes the audio stack: `nomad-voice` handles *speech*
(TTS/STT); this handles *music + sound*.

Like ComfyUI, it's a **host-native GPU service** (its own venv, not a container) so the heavy
`torch` + `acestep` deps stay out of the slim engine image. The console proxies it (localhost),
and the v2 engine reaches it at `host.docker.internal:8220`.

| Endpoint | Body | Returns |
|---|---|---|
| `GET /health` | — | `{status, backend, bf16, loaded}` |
| `POST /music` | `{prompt, lyrics?, duration?, steps?, guidance_scale?, seed?}` | `audio/wav` |

- `prompt` = **style tags**, e.g. `"lofi hip hop, chill, mellow piano, 90 bpm"`.
- `lyrics` = optional; use `[verse]` / `[chorus]` tags for structure (leave empty for instrumental).
- ACE-Step generates a full track with vocals + instruments; 50+ languages; fast on the 5090.

## Install

```bash
nomad-audio/install.sh            # venv + CUDA torch (cu128) + ACE-Step + service deps
nomad-audio/install_service.sh    # optional: run on boot (systemd user unit, :8220)
# manual start instead of the service:
nomad-audio/.venv/bin/python nomad-audio/server.py
```

The first `POST /music` auto-downloads the ACE-Step checkpoints (~a few GB) to
`~/.cache/ace-step/checkpoints`.

## Try it

```bash
curl -s -X POST http://127.0.0.1:8220/music \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"lofi hip hop, chill, mellow piano, rain, 80 bpm","duration":20}' \
  -o /tmp/nomad_music.wav && echo "wrote /tmp/nomad_music.wav"
```

## Environment

| Var | Default | Meaning |
|---|---|---|
| `NOMAD_AUDIO_PORT` | `8220` | Listen port. |
| `ACESTEP_CHECKPOINT` | *(auto)* | Checkpoint dir; empty = auto-download to `~/.cache/ace-step`. |
| `ACESTEP_BF16` | `1` | bf16 precision (lower VRAM). |
| `ACESTEP_CPU_OFFLOAD` | `0` | Offload to system RAM if VRAM is tight. |
| `ACESTEP_STEPS` / `ACESTEP_GUIDANCE` / `ACESTEP_DURATION` | `60` / `15` / `30` | Generation defaults (per-request overridable). |
| `NOMAD_AUDIO_DIR` | `./generated_audio` | Where wavs are written. |

## Notes

- **License:** ACE-Step is **Apache-2.0** — safe for commercial/monetized output (unlike FLUX
  `[dev]` for images). For *sound effects / short-form* audio, **Stable Audio Open** is the planned
  complement (a later `/sfx` endpoint).
- **ACE-Step 1.5** (newer, faster) can be swapped in via `ACESTEP_REPO` in `install.sh` once its
  Python API is confirmed against this pipeline; the checkpoint is env-configurable.
- Shares the GPU with ComfyUI (images). ACE-Step is light (bf16, `cpu_offload` available), so they
  coexist; if VRAM is tight, set `ACESTEP_CPU_OFFLOAD=1`.
- **Host gotchas (handled by `install.sh` / the service):** `torchvision` must come from the cu128
  index (a CPU build breaks transformers with `torchvision::nms does not exist`); and `torchaudio`'s
  save routes through `torchcodec`, which has no build for FFmpeg 8 — the service installs
  `soundfile` and patches `torchaudio.save` to write wavs directly. Manage the service with
  `systemctl --user {status|restart} nomad-audio`.

Verified end-to-end: a text prompt → a 9.9s stereo 48 kHz WAV, saved to `generated_audio/`.
