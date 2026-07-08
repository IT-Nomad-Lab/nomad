"""NOMAD Audio — local music / sound generation (ACE-Step).

Keeps NOMAD's audio fully on-device: no cloud music APIs. Runs NATIVELY on the host GPU
(like ComfyUI) — heavy deps (torch + acestep) live in this service's own venv. The console
proxies to it (localhost-bound), and the v2 engine can reach it at host.docker.internal:8220.

  POST /music  {prompt, lyrics?, duration?, steps?, guidance_scale?, seed?}  -> audio/wav
  GET  /health

`prompt` = style tags (e.g. "lofi hip hop, chill, mellow piano, 90 bpm"); `lyrics` optional
(use [verse]/[chorus] tags for structure). ACE-Step is Apache-2.0 — commercial-safe.

Backend/checkpoint are env-configurable so the model can be swapped (e.g. to ACE-Step 1.5)
without touching this code.
"""
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

# Empty checkpoint dir → acestep auto-downloads to ~/.cache/ace-step/checkpoints on first load.
CKPT_DIR = os.environ.get("ACESTEP_CHECKPOINT", "").strip()
BF16 = os.environ.get("ACESTEP_BF16", "1").lower() not in ("0", "false", "no")
CPU_OFFLOAD = os.environ.get("ACESTEP_CPU_OFFLOAD", "0").lower() not in ("0", "false", "no")
TORCH_COMPILE = os.environ.get("ACESTEP_TORCH_COMPILE", "0").lower() not in ("0", "false", "no")
OUT_DIR = os.environ.get("NOMAD_AUDIO_DIR",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_audio"))
os.makedirs(OUT_DIR, exist_ok=True)

# Generation defaults (all overridable per request); mirror ACE-Step's reference infer settings.
DEF_STEPS = int(os.environ.get("ACESTEP_STEPS", "60"))
DEF_GUIDANCE = float(os.environ.get("ACESTEP_GUIDANCE", "15"))
DEF_DURATION = float(os.environ.get("ACESTEP_DURATION", "30"))
SCHEDULER = os.environ.get("ACESTEP_SCHEDULER", "euler")

app = FastAPI(title="NOMAD Audio")
_pipe = None


def _patch_torchaudio_save():
    """ACE-Step saves via torchaudio.save(); torchaudio 2.x routes that through torchcodec, which
    has no build for the host's FFmpeg 8 (fails to load). Replace it with a soundfile writer so
    output never touches torchcodec. torchaudio uses [channels, frames]; soundfile wants
    [frames, channels]."""
    import numpy as np
    import soundfile as sf
    import torchaudio

    def _save(uri, src, sample_rate=48000, **_kw):
        a = src.detach().cpu().numpy() if hasattr(src, "detach") else np.asarray(src)
        a = np.squeeze(a)                                   # drop batch dims → [frames] or [ch, frames]
        if a.ndim == 2 and a.shape[0] <= 8 and a.shape[0] < a.shape[1]:
            a = a.T                                         # [channels, frames] → [frames, channels]
        sf.write(str(uri), a, int(sample_rate))

    torchaudio.save = _save


def pipe():
    """Lazy-load the ACE-Step pipeline (first call warms it; reused after)."""
    global _pipe
    if _pipe is None:
        from acestep.pipeline_ace_step import ACEStepPipeline
        _patch_torchaudio_save()   # bypass torchcodec (no FFmpeg-8 build) → save via soundfile
        _pipe = ACEStepPipeline(
            checkpoint_dir=CKPT_DIR or None,
            dtype="bfloat16" if BF16 else "float32",
            torch_compile=TORCH_COMPILE,
            cpu_offload=CPU_OFFLOAD,
            overlapped_decode=False,
        )
    return _pipe


@app.get("/health")
def health():
    return {"status": "ok", "backend": "ace-step", "bf16": BF16,
            "cpu_offload": CPU_OFFLOAD, "loaded": _pipe is not None}


def _resolve_output(expected: str) -> str:
    """ACE-Step may write to `expected` or a suffixed variant; return the actual wav path."""
    if os.path.exists(expected):
        return expected
    stem = os.path.splitext(os.path.basename(expected))[0]
    cands = [os.path.join(OUT_DIR, f) for f in os.listdir(OUT_DIR)
             if f.startswith(stem) and f.lower().endswith((".wav", ".mp3", ".flac"))]
    if cands:
        return max(cands, key=os.path.getmtime)
    raise FileNotFoundError(f"no audio produced for {expected}")


@app.post("/music")
async def music(req: Request):
    body = await req.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt (style tags) required"}, status_code=400)
    lyrics = (body.get("lyrics") or "").strip()
    duration = float(body.get("duration", DEF_DURATION))
    steps = int(body.get("steps", DEF_STEPS))
    guidance = float(body.get("guidance_scale", DEF_GUIDANCE))
    seed = body.get("seed")

    out = os.path.join(OUT_DIR, f"music_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav")
    t0 = time.time()
    try:
        pipe()(
            audio_duration=duration,
            prompt=prompt,
            lyrics=lyrics,
            infer_step=steps,
            guidance_scale=guidance,
            scheduler_type=SCHEDULER,
            cfg_type="apg",
            omega_scale=10.0,
            manual_seeds=(str(seed) if seed is not None else None),
            guidance_interval=0.5,
            guidance_interval_decay=0.0,
            min_guidance_scale=3.0,
            use_erg_tag=True,
            use_erg_lyric=False,
            use_erg_diffusion=True,
            oss_steps="",
            guidance_scale_text=0.0,
            guidance_scale_lyric=0.0,
            save_path=out,
        )
        path = _resolve_output(out)
    except Exception as e:
        return JSONResponse({"error": str(e)[:500]}, status_code=500)
    dt = round(time.time() - t0, 1)
    return FileResponse(path, media_type="audio/wav", filename=os.path.basename(path),
                        headers={"X-Generation-Seconds": str(dt)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("NOMAD_AUDIO_HOST", "0.0.0.0"),
                port=int(os.environ.get("NOMAD_AUDIO_PORT", "8220")))
