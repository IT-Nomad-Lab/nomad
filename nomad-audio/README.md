# nomad-audio — local music & sound generation

On-device music and sound generation for NOMAD. Music is **[ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5)**
(MIT, **commercial-safe**), with ACE-Step v1 (Apache-2.0) kept as a one-variable rollback. Short sound
effects are **[Stable Audio Open 1.0](https://huggingface.co/stabilityai/stable-audio-open-1.0)**
(different licence, see below). Songs with vocals, from style tags and lyrics, are
**[HeartMuLa 3B](https://github.com/HeartMuLa/heartlib)** (Apache-2.0, commercial-safe). Completes the audio stack: `nomad-voice` handles *speech*
(TTS/STT); this handles *music + sound*.

Like ComfyUI, it's a **host-native GPU service** (its own venv, not a container) so the heavy
`torch` + `acestep` deps stay out of the slim engine image. The console proxies it (localhost),
and the v2 engine reaches it at `host.docker.internal:8220`.

| Endpoint | Body | Returns |
|---|---|---|
| `GET /health` | — | `{status, backend, ace_step_version, acestep_package, model, bf16, cpu_offload, loaded, sfx{model, loaded, max_duration_s}, song{model, installed, venv, ckpt, mode, full_mib, lazy_mib, guard_need_mib, max_seconds}, gpu_free_gb}` |
| `POST /music` | `{prompt, lyrics?, duration?, steps?, guidance_scale?, seed?}` | `audio/wav` (48 kHz stereo) |
| `POST /sfx` | `{prompt, duration_s?, seed?, steps?, negative_prompt?}` | `audio/wav` (44.1 kHz stereo) |
| `POST /song` | `{tags, lyrics, max_seconds?, seed?, cfg_scale?, temperature?, topk?}` | `audio/wav` (48 kHz stereo, 24-bit) |

Every audio endpoint answers with the WAV body plus headers: `X-Generation-Seconds`,
`X-Audio-Path` (the copy kept in `generated_audio/`) and `X-Model`. `/sfx` and `/song` add `X-Seed`
(the seed used, random if you sent none), `X-Duration-Seconds` and `X-Sample-Rate`; `/song` also
sends `X-Song-Mode` (`full` or `lazy`). Errors are JSON `{"error": …}` with 400 for a bad request,
503 when `/song` can't start (GPU too full, or the engine not installed) and 500 for a failed
generation.

- `prompt` (music) = **style tags**, e.g. `"lofi hip hop, chill, mellow piano, 90 bpm"`.
- `lyrics` = optional; use `[verse]` / `[chorus]` tags for structure (leave empty for instrumental).
- `prompt` (sfx) = a plain description, e.g. `"a wooden door creaks open slowly"`.
- `duration_s` (sfx) defaults to 5 and is capped at **47 s**, the model's ceiling; over that is a 400.
- `/song`: `tags` and `lyrics` are both required. Lyrics use `[Verse]` / `[Chorus]` / `[Bridge]` /
  `[Outro]` lines; the language is whatever you write (EN, ES and ZH tested in #1417). The song
  ends when the model ends it; `max_seconds` (10–240, default 240) only caps it. Defaults:
  `cfg_scale` 3.0, `temperature` 1.0, `topk` 50. A 2-minute song takes about 2.5 minutes plus a
  32 s load.
- `steps` on `/music` defaults to 8 on 1.5 turbo and 60 on v1; `guidance_scale` is passed only when
  you send it on 1.5.

## Install

```bash
nomad-audio/install.sh                     # ACE-Step 1.5 → ~/.local/share/nomad-audio/ace-step-1.5 (tag v0.1.8, uv sync)
ACE_STEP_VERSION=1 nomad-audio/install.sh  # ACE-Step v1  → .venv  (keep it for rollback)
nomad-audio/install_service.sh             # run on boot (systemd user unit, :8220)
# manual start instead of the service:
nomad-audio/run.sh
```

v1 and 1.5 both install a top-level `acestep` package, so each gets its own venv. `run.sh` picks
the venv from `ACE_STEP_VERSION` and refuses to start if that venv is missing, rather than
quietly running the other version. Both venvs get the `/sfx` deps from `requirements.txt`.

The first `POST /music` on 1.5 downloads its checkpoints to `~/.local/share/nomad-audio/ace-step-1.5/checkpoints`
(gitignored); on v1 they go to `~/.cache/ace-step/checkpoints`. The first `POST /sfx` downloads
Stable Audio Open to the Hugging Face cache. The service starts and serves `/music` without it.

### Stable Audio Open: licence and Hugging Face access

`stabilityai/stable-audio-open-1.0` is **gated**. Once, before the first `/sfx`:

1. Accept the licence on the model page while logged in to Hugging Face.
2. Log in on this machine: `~/.local/share/nomad-audio/ace-step-1.5/.venv/bin/huggingface-cli login` (the token goes to
   `~/.cache/huggingface/token`, which diffusers reads). Or put `HF_TOKEN` in the unit with
   `systemctl --user edit nomad-audio`. Never commit a token.

Licence: the **Stability AI Community License**, not MIT/Apache-2.0. Commercial use is free while
you (with affiliates) make under US $1,000,000 a year in revenue; above that the licence ends and
an enterprise licence is needed (checked 2026-10-04). Re-read the terms on the model page before
shipping SFX in something you sell. Without access, `/sfx` returns a
500 that says how to fix it.

### Stable Audio 3 (/music, default engine): install and licence

`POST /music` takes `engine`: `sa3` (default, Stable Audio 3 `medium`, up to 380 s, 44.1 kHz
stereo, negative prompt "poor quality, vocals") or `ace-step`. SA3 runs `sa3_worker.py` as a
one-shot subprocess in its own venv (Python 3.10, torch 2.7.0+cu128, flash-attn 2.8.3) at
`~/music-engines/sa3/.venv`; override with `SA3_ROOT` / `SA3_PYTHON` / `SA3_MODEL`, and
`NOMAD_AUDIO_MUSIC_ENGINE` changes the default. It peaks near 5.2 GB of VRAM and answers 503
when less than `SA3_MIN_FREE_MIB` (6200) is free, so run it under gpu_guard. Output is 44.1 kHz;
the content backend resamples to 48 kHz.

Licence: the **Stability AI Community License**. Free for commercial use while you (with
affiliates) make under US $1,000,000 a year in revenue; above that an enterprise licence is needed.

### HeartMuLa (/song): install and GPU

```bash
nomad-audio/install.sh heartmula     # venv + heartlib 5858ca8 + ~21 GB of checkpoints
# HEARTMULA_CKPT_FROM=<dir> hard-links checkpoints you already have instead of downloading them
systemctl --user restart nomad-audio # /health: "song": {"installed": true, ...}
```

heartlib pins `transformers`, `torchtune` and `pyarrow` versions that clash with ACE-Step, so it
lives in its own venv at `~/.local/share/nomad-audio/heartmula/.venv`. Each `/song` drops the
service's resident music or SFX model and runs `song_worker.py` there as a one-shot subprocess.
The weights load, the song is made, and the process exits, so the VRAM comes back.

HeartMuLa needs most of the card, which `private` and STT normally fill. `/song` reads free VRAM
first: **23,500 MiB** or more loads both modules at once (23.3 GiB peak). **14,000 MiB** or more
loads them one at a time (`lazy`: 12.8 GiB peak, about 2.3× slower). Below that it answers 503
and loads nothing. The same seed usually gives the same song, bit for bit, but not always: one run
in four of a 152 s song came out different (#1427), so keep the file if you need it again. To free
the card, run the call under the backend's guard.
Ask it for **22,800 MiB**: the guard adds a 1,024 MiB margin and can free only about 23,900 MiB, so
`--need-mib 23500` is always refused (rc 75). What it frees is still enough for full load:

```bash
cd ~/content-creation/backend && python3 -m media_worker.gpu_guard run --need-mib 22800 \
  --stop nomad-stt -- sh -c 'curl -s -X POST http://127.0.0.1:8220/song -H "Content-Type: application/json" \
  -H "X-Nomad-Gpu-Lease: $NOMAD_GPU_LEASE" \
  -d "{\"tags\":\"latin pop, male vocal, acoustic guitar\",\"lyrics\":\"[Verse]\\n...\\n[Chorus]\\n...\"}" -o /tmp/song.wav'
```

The guard holds the shared GPU admission for the whole run, so the request must carry its lease
(`$NOMAD_GPU_LEASE`, set by `gpu_guard run`); without it this service answers 503.

## Try it

```bash
curl -s -X POST http://127.0.0.1:8220/music \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"lofi hip hop, chill, mellow piano, rain, 80 bpm","duration":20}' \
  -o /tmp/nomad_music.wav && echo "wrote /tmp/nomad_music.wav"

curl -s -X POST http://127.0.0.1:8220/sfx \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a wooden door creaks open slowly","duration_s":3}' \
  -o /tmp/nomad_sfx.wav && echo "wrote /tmp/nomad_sfx.wav"
```

Smoke test against the running service (hits the GPU, downloads models on first run):

```bash
~/.local/share/nomad-audio/ace-step-1.5/.venv/bin/python nomad-audio/smoke_test.py --expect-version 1.5
```

## Rollback to v1

```bash
systemctl --user edit nomad-audio     # add:  [Service]
                                      #       Environment=ACE_STEP_VERSION=1
systemctl --user restart nomad-audio
curl -s http://127.0.0.1:8220/health  # "ace_step_version": "1"
```

Remove the override and restart to go back to 1.5. Nothing else changes: same port, same
`/music` contract, same MCP tools. `/sfx` works under either version.

## Environment

| Var | Default | Meaning |
|---|---|---|
| `ACE_STEP_VERSION` | `1.5` | Music backend: `1.5` or `1`. Also picks the venv in `run.sh`. |
| `NOMAD_AUDIO_PORT` | `8220` | Listen port. |
| `NOMAD_AUDIO_DIR` | `./generated_audio` | Where wavs are written. |
| `NOMAD_AUDIO_MAX_SECONDS` | `240` | `/music` duration cap; longer requests get a 400. |
| `ACESTEP15_PROJECT_ROOT` | `~/.local/share/nomad-audio/ace-step-1.5` | 1.5 clone (from `install.sh`); holds its `.venv` and `checkpoints/`. |
| `ACESTEP15_CONFIG` | `acestep-v15-turbo` | 1.5 DiT config. |
| `ACESTEP15_STEPS` | `8` | 1.5 default inference steps. |
| `ACESTEP15_LM` | *(off)* | Optional 5Hz LM, e.g. `acestep-5Hz-lm-0.6B`. Off = DiT only, less VRAM. |
| `ACESTEP15_LM_BACKEND` | `pt` | LM backend when the LM is on. |
| `ACESTEP_CHECKPOINT` | *(auto)* | v1 checkpoint dir; empty = auto-download to `~/.cache/ace-step`. |
| `ACESTEP_BF16` | `1` | v1 bf16 precision (lower VRAM). |
| `ACESTEP_CPU_OFFLOAD` | `0` | Offload to system RAM if VRAM is tight (v1, and 1.5 where supported). |
| `ACESTEP_STEPS` / `ACESTEP_GUIDANCE` / `ACESTEP_DURATION` | `60` / `15` / `30` | v1 generation defaults; `DURATION` is the default for both. |
| `SAO_MODEL` | `stabilityai/stable-audio-open-1.0` | SFX model. |
| `SAO_STEPS` / `SAO_DURATION` / `SAO_MAX_SECONDS` | `100` / `5` / `47` | SFX defaults and cap. |
| `HEARTMULA_ROOT` | `~/.local/share/nomad-audio/heartmula` | `/song` engine: holds `.venv`, `heartlib/` and `ckpt/` (from `install.sh heartmula`). |
| `HEARTMULA_MODE` | `auto` | `auto` picks full or lazy by free VRAM; `full` or `lazy` forces one (still 503 when short). |
| `HEARTMULA_FULL_MIB` / `HEARTMULA_LAZY_MIB` | `23500` / `14000` | Free VRAM needed for each mode (measured in #1419). |

## Notes

- **Shared GPU admission (#1462):** each render takes `/tmp/nomad-gpu.lock` without waiting
  (`dispatcher/gpu_admission.py`), or runs on the lease of the gpu_guard that holds it
  (`X-Nomad-Gpu-Lease`, checked against the guard's lease file and `/proc/locks`). Busy or a
  pending guard restore answers 503 with `Retry-After` and loads nothing. Under its own admission
  the service drops its models before releasing, so direct calls reload each time. Deploy order:
  the content backend must send the lease (backend main 834f851) before this service is restarted
  with admission, or guarded music jobs get 503 and are deferred until it does.
- **GPU sharing:** one lock serializes every generation, so a music and an sfx request never run
  at once; the second waits. Only one audio model is resident: `/music` unloads the SFX model
  and `/sfx` unloads the music model before loading. `private` (22 GB) and ComfyUI share the card,
  so expect a reload when you alternate under pressure.
- **1.5 API drift:** the 1.5 adapter checks the installed `GenerationParams` / `GenerationConfig`
  fields at call time. A missing required field is an error, not a silent drop. A pinned seed the
  installed version can't take is also refused rather than returning a random take.
- **Host gotchas (handled by `install.sh` / the service):** `torchvision` must come from the cu128
  index (a CPU build breaks transformers with `torchvision::nms does not exist`); and `torchaudio`'s
  save routes through `torchcodec`, which has no build for FFmpeg 8, so the service installs
  `soundfile` and patches `torchaudio.save` to write wavs directly. `/sfx` needs `torchsde`.
  Manage the service with `systemctl --user {status|restart} nomad-audio`.

Verified end-to-end on v1 (2026-07): a text prompt → a 9.9s stereo 48 kHz WAV, saved to
`generated_audio/`. The 1.5 path and `/sfx` have not yet been run on this machine.
