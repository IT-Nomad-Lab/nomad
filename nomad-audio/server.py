"""NOMAD Audio — local music / sound / song generation (ACE-Step, Stable Audio Open, HeartMuLa).

Keeps NOMAD's audio fully on-device: no cloud music APIs. Runs NATIVELY on the host GPU
(like ComfyUI) — heavy deps (torch + acestep) live in this service's own venv. The console
proxies to it (localhost-bound), and the v2 engine can reach it at host.docker.internal:8220.

  POST /music  {prompt, lyrics?, duration?, steps?, guidance_scale?, seed?, engine?}  -> audio/wav
               engine "sa3" (default, Stable Audio 3, up to 380 s, 44.1 kHz) or "ace-step"
  POST /sfx    {prompt, duration_s?, seed?, steps?, negative_prompt?}         -> audio/wav
  POST /song   {tags, lyrics, max_seconds?, seed?, cfg_scale?, temperature?, topk?} -> audio/wav
  GET  /health

`prompt` = style tags (e.g. "lofi hip hop, chill, mellow piano, 90 bpm"); `lyrics` optional
(use [verse]/[chorus] tags for structure). ACE-Step 1.5 is MIT, v1 Apache-2.0 — both commercial-safe.

ACE_STEP_VERSION picks the music backend: "1.5" (default) or "1" (rollback). Both releases
install a top-level `acestep` package, so each lives in its own venv and run.sh picks the venv
from the same variable. /sfx is Stable Audio Open via diffusers, loaded on first use only.

/song is HeartMuLa 3B (Apache-2.0): a song with vocals from style tags and lyrics. Its pins clash
with ACE-Step's venv, so it runs song_worker.py as a one-shot subprocess in its own venv
(install.sh heartmula), after dropping this process's models. It needs most of the card: run it
under the backend's gpu_guard, or it answers 503 without loading anything.

GPU admission (#1462): every render holds /tmp/nomad-gpu.lock (dispatcher/gpu_admission.py), or
runs under the lease of the gpu_guard that holds it for the caller. Busy answers 503 with
Retry-After and loads nothing. A render under this service's own admission drops its models
before it releases admission, so direct callers (console, engine) pay a reload each time.
"""
import dataclasses
import gc
import importlib.util
import inspect
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

HERE = os.path.dirname(os.path.abspath(__file__))
_VERSIONS = {"1": "1", "v1": "1", "1.0": "1", "1.5": "1.5", "v1.5": "1.5", "15": "1.5"}
ACE_STEP_VERSION = _VERSIONS.get(os.environ.get("ACE_STEP_VERSION", "1.5").strip().lower())
if ACE_STEP_VERSION is None:
    sys.exit(f"ACE_STEP_VERSION={os.environ.get('ACE_STEP_VERSION')!r}: use '1.5' or '1'")

# Empty checkpoint dir → acestep auto-downloads to ~/.cache/ace-step/checkpoints on first load.
CKPT_DIR = os.environ.get("ACESTEP_CHECKPOINT", "").strip()
BF16 = os.environ.get("ACESTEP_BF16", "1").lower() not in ("0", "false", "no")
CPU_OFFLOAD = os.environ.get("ACESTEP_CPU_OFFLOAD", "0").lower() not in ("0", "false", "no")
TORCH_COMPILE = os.environ.get("ACESTEP_TORCH_COMPILE", "0").lower() not in ("0", "false", "no")
_spec = importlib.util.spec_from_file_location(   # by path, like bridge_bind: this venv has no repo on sys.path
    "nomad_gpu_admission", os.path.join(HERE, "..", "dispatcher", "gpu_admission.py"))
gpu_admission = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gpu_admission)
BUSY_RETRY_S = 30

OUT_DIR = os.environ.get("NOMAD_AUDIO_DIR", os.path.join(HERE, "generated_audio"))
os.makedirs(OUT_DIR, exist_ok=True)

# Generation defaults (all overridable per request); mirror ACE-Step's reference infer settings.
DEF_STEPS = int(os.environ.get("ACESTEP_STEPS", "60"))
DEF_GUIDANCE = float(os.environ.get("ACESTEP_GUIDANCE", "15"))
DEF_DURATION = float(os.environ.get("ACESTEP_DURATION", "30"))
SCHEDULER = os.environ.get("ACESTEP_SCHEDULER", "euler")
MUSIC_MAX_S = float(os.environ.get("NOMAD_AUDIO_MAX_SECONDS", "240"))   # the MCP tool caps at 240 too

# ACE-Step 1.5. The project root is install.sh's clone (outside the repo); it holds the venv and
# checkpoints/, which 1.5 downloads on first load.
ACESTEP15_ROOT = os.environ.get("ACESTEP15_PROJECT_ROOT",
                                os.path.expanduser("~/.local/share/nomad-audio/ace-step-1.5"))
ACESTEP15_CONFIG = os.environ.get("ACESTEP15_CONFIG", "acestep-v15-turbo")
ACESTEP15_STEPS = int(os.environ.get("ACESTEP15_STEPS", "8"))           # turbo is distilled to ~8
ACESTEP15_LM = os.environ.get("ACESTEP15_LM", "").strip()               # e.g. acestep-5Hz-lm-0.6B
ACESTEP15_LM_BACKEND = os.environ.get("ACESTEP15_LM_BACKEND", "pt")

# Stable Audio Open (/sfx). Gated on Hugging Face: accept the licence once and log in
# (`huggingface-cli login` or HF_TOKEN in the service env). Never hardcode a token here.
SAO_MODEL = os.environ.get("SAO_MODEL", "stabilityai/stable-audio-open-1.0")
SAO_STEPS = int(os.environ.get("SAO_STEPS", "100"))
SAO_MAX_S = float(os.environ.get("SAO_MAX_SECONDS", "47"))   # the 1.0 model's hard ceiling is ~47 s
SAO_DEF_S = float(os.environ.get("SAO_DURATION", "5"))

# HeartMuLa (/song). install.sh heartmula builds the venv and checkpoints under HEARTMULA_ROOT.
# Free VRAM picks the load mode (#1417/#1419, measured under gpu_guard: 23,840-23,918 MiB free):
# full load peaks at 23,852 MiB torch-allocated and needs the whole card; lazy load (one module at a
# time, 2.3x slower) peaks at 13,139 MiB. Below the lazy floor /song refuses with a 503.
HEARTMULA_ROOT = os.environ.get("HEARTMULA_ROOT",
                                os.path.expanduser("~/.local/share/nomad-audio/heartmula"))
HEARTMULA_PYTHON = os.environ.get("HEARTMULA_PYTHON", os.path.join(HEARTMULA_ROOT, ".venv", "bin", "python"))
HEARTMULA_CKPT = os.environ.get("HEARTMULA_CKPT", os.path.join(HEARTMULA_ROOT, "ckpt"))
HEARTMULA_MODE = os.environ.get("HEARTMULA_MODE", "auto").strip().lower()   # auto | full | lazy
SONG_FULL_MIB = int(os.environ.get("HEARTMULA_FULL_MIB", "23500"))
SONG_LAZY_MIB = int(os.environ.get("HEARTMULA_LAZY_MIB", "14000"))
# What a caller asks gpu_guard for: the guard adds a 1,024 MiB margin and frees about 23,900 MiB,
# so asking for SONG_FULL_MIB itself is always refused. 22,800 + margin fits and leaves full load.
SONG_GUARD_MIB = 22800
SONG_MAX_LYRICS = int(os.environ.get("HEARTMULA_MAX_LYRICS", "5000"))
SONG_MODEL = "heartmula-oss-3b"
SONG_WORKER = os.path.join(HERE, "song_worker.py")
_CKPT_PARTS = ("HeartMuLa-oss-3B", "HeartCodec-oss", "tokenizer.json", "gen_config.json")

# Stable Audio 3 (/music engine "sa3", the default, #1343). Own venv (Python 3.10, torch 2.7.0+cu128,
# flash-attn), run as a one-shot subprocess like HeartMuLa. Community License: free under $1M revenue.
MUSIC_ENGINES = ("sa3", "ace-step")
MUSIC_ENGINE = os.environ.get("NOMAD_AUDIO_MUSIC_ENGINE", "sa3").strip().lower()
SA3_ROOT = os.environ.get("SA3_ROOT", os.path.expanduser("~/music-engines/sa3"))
SA3_PYTHON = os.environ.get("SA3_PYTHON", os.path.join(SA3_ROOT, ".venv", "bin", "python"))
SA3_MODEL = os.environ.get("SA3_MODEL", "medium")
SA3_MAX_S = float(os.environ.get("SA3_MAX_SECONDS", "380"))     # medium's ceiling
SA3_NEGATIVE = "poor quality, vocals"
SA3_MIN_FREE_MIB = int(os.environ.get("SA3_MIN_FREE_MIB", "6200"))   # peaks near 5.2 GB, plus margin
SA3_WORKER = os.path.join(HERE, "sa3_worker.py")

# One GPU, two models, one resident at a time: requests run one at a time, and before /music or
# /sfx loads its model the other one is dropped (ComfyUI and Ollama's `private` share the card).

app = FastAPI(title="NOMAD Audio")
_gpu = threading.Lock()
_pipe = None      # ACE-Step v1 pipeline
_ace15 = None     # (dit_handler, llm_handler) for 1.5
_sao = None       # Stable Audio Open pipeline


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


def _free_gb():
    """Free VRAM in GB, or None when torch isn't loaded yet (don't init CUDA just to ask)."""
    if "torch" not in sys.modules:
        return None
    import torch
    if not torch.cuda.is_available():
        return None
    free, _total = torch.cuda.mem_get_info()
    return round(free / 2**30, 1)


def _make_room(for_kind: str):
    """Drop the *other* model, so only one audio model is ever resident. Caller holds _gpu."""
    global _pipe, _ace15, _sao
    if for_kind == "sa3" and (_pipe is not None or _ace15 is not None or _sao is not None):
        _pipe = _ace15 = _sao = None   # SA3 runs in a subprocess; free what this process holds
    elif for_kind == "song" and (_pipe is not None or _ace15 is not None or _sao is not None):
        _pipe = _ace15 = _sao = None   # HeartMuLa runs in a subprocess and needs the whole card
    elif for_kind == "sfx" and (_pipe is not None or _ace15 is not None):
        _pipe = _ace15 = None
    elif for_kind == "music" and _sao is not None:
        _sao = None
    else:
        return
    gc.collect()
    import torch
    torch.cuda.empty_cache()


def _drop_models():
    """Drop every model this process holds, so nothing stays resident after admission ends."""
    global _pipe, _ace15, _sao
    with _gpu:
        if _pipe is None and _ace15 is None and _sao is None:
            return
        _pipe = _ace15 = _sao = None
        gc.collect()
        if "torch" in sys.modules:
            sys.modules["torch"].cuda.empty_cache()


def _admitted(token, render, *args, **kw):
    """One render under shared GPU admission (#1462): this process's own, never waiting, or the
    lease of the gpu_guard that made room for the caller (header X-Nomad-Gpu-Lease). Under its own
    admission the models are dropped before admission is released, because nothing else would
    evict them; under a lease the guard stops this service when its job ends."""
    with gpu_admission.request_admission(token) as how:
        try:
            return render(*args, **kw)
        finally:
            if how == "own":
                _drop_models()


def _busy(e):
    return JSONResponse({"error": str(e)}, status_code=503, headers={"Retry-After": str(BUSY_RETRY_S)})


def pipe():
    """Lazy-load the ACE-Step v1 pipeline (first call warms it; reused after)."""
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


def _fit(target, required: dict, optional: dict) -> dict:
    """Keyword args for `target` (a function or dataclass). A required name it doesn't accept is
    an error, never a silent drop; optional names it doesn't know are left out. This guards
    against drift in the 1.5 API between tags."""
    if dataclasses.is_dataclass(target):
        names, open_kw = {f.name for f in dataclasses.fields(target)}, False
    else:
        params = inspect.signature(target).parameters
        names = set(params)
        open_kw = any(p.kind is p.VAR_KEYWORD for p in params.values())
    missing = [k for k in required if not open_kw and k not in names]
    if missing:
        raise RuntimeError(f"installed acestep {getattr(target, '__name__', target)} has no "
                           f"{missing}; this server targets ACE-Step 1.5 (see install.sh pin)")
    out = dict(required)
    out.update({k: v for k, v in optional.items() if open_kw or k in names})
    return out


def _check_init(ret, what: str):
    """1.5's initializers report failure in their return value, not by raising."""
    if isinstance(ret, tuple) and len(ret) >= 2 and ret[1] is False:
        raise RuntimeError(f"{what} failed: {str(ret[0])[:300]}")


def ace15():
    """Lazy-load the ACE-Step 1.5 DiT handler (+ the optional 5Hz LM)."""
    global _ace15
    if _ace15 is None:
        from acestep.handler import AceStepHandler
        from acestep.llm_inference import LLMHandler
        _patch_torchaudio_save()
        dit = AceStepHandler()
        _check_init(dit.initialize_service(**_fit(
            dit.initialize_service,
            {"project_root": ACESTEP15_ROOT, "config_path": ACESTEP15_CONFIG, "device": "cuda"},
            {"offload_to_cpu": CPU_OFFLOAD})), "ACE-Step 1.5 DiT init")
        llm = LLMHandler()
        if ACESTEP15_LM:
            _check_init(llm.initialize(**_fit(
                llm.initialize,
                {"checkpoint_dir": os.path.join(ACESTEP15_ROOT, "checkpoints"),
                 "lm_model_path": ACESTEP15_LM, "backend": ACESTEP15_LM_BACKEND,
                 "device": "cuda"}, {})), "ACE-Step 1.5 LM init")
        _ace15 = (dit, llm)
    return _ace15


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


def _music_v1(out, prompt, lyrics, duration, steps, guidance, seed):
    pipe()(
        audio_duration=duration,
        prompt=prompt,
        lyrics=lyrics,
        infer_step=steps if steps is not None else DEF_STEPS,
        guidance_scale=guidance if guidance is not None else DEF_GUIDANCE,
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
    return _resolve_output(out)


def _music_v15(out, prompt, lyrics, duration, steps, guidance, seed):
    from acestep.inference import GenerationConfig, GenerationParams, generate_music
    dit, llm = ace15()
    p_opt = {"inference_steps": steps if steps is not None else ACESTEP15_STEPS,
             "thinking": bool(ACESTEP15_LM)}
    if guidance is not None:
        p_opt["guidance_scale"] = guidance
    c_opt = {"batch_size": 1}
    if seed is not None:
        p_opt["seed"] = int(seed)
        c_opt.update(use_random_seed=False, seeds=[int(seed)])
    p_kw = _fit(GenerationParams, {"caption": prompt, "lyrics": lyrics, "duration": duration}, p_opt)
    c_kw = _fit(GenerationConfig, {"audio_format": "wav"}, c_opt)
    if seed is not None and "seed" not in p_kw and "seeds" not in c_kw:
        raise RuntimeError("installed ACE-Step 1.5 takes no seed; refusing to return a random take")
    params, config = GenerationParams(**p_kw), GenerationConfig(**c_kw)

    work = os.path.join(OUT_DIR, f".v15_{uuid.uuid4().hex[:8]}")
    os.makedirs(work, exist_ok=True)
    try:
        result = generate_music(dit, llm, params, config, save_dir=work)
        if not getattr(result, "success", True):
            raise RuntimeError(str(getattr(result, "error", None)
                                   or getattr(result, "status_message", "generation failed")))
        audios = getattr(result, "audios", None) or []
        first = audios[0] if audios else None
        src = (first.get("path") if isinstance(first, dict) else getattr(first, "path", None))
        if not src or not os.path.exists(src):
            found = [os.path.join(work, f) for f in os.listdir(work)
                     if f.lower().endswith((".wav", ".flac", ".mp3"))]
            src = max(found, key=os.path.getmtime) if found else None
        if not src:
            raise FileNotFoundError("ACE-Step 1.5 reported no audio file")
        if src.lower().endswith(".wav"):
            shutil.move(src, out)
        else:                                   # asked for wav; convert if it gave something else
            import soundfile as sf
            data, sr = sf.read(src)
            sf.write(out, data, sr)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


def _render_sa3(out, prompt, duration, seed, **_ignored):
    """Run sa3_worker.py once in the SA3 venv. Raises SongUnavailable when it can't run now."""
    if not os.path.exists(SA3_PYTHON):
        raise SongUnavailable(f"Stable Audio 3 not installed ({SA3_PYTHON} missing); set SA3_ROOT")
    seed = random.randrange(2**31) if seed is None else seed
    req = {"model": SA3_MODEL, "out": out, "prompt": prompt, "negative_prompt": SA3_NEGATIVE,
           "duration": duration, "seed": seed}
    with _gpu:
        _make_room("sa3")
        free = _free_mib()
        if free is None or free < SA3_MIN_FREE_MIB:
            raise SongUnavailable(f"Stable Audio 3 needs {SA3_MIN_FREE_MIB} MiB free VRAM, "
                                  f"{free} MiB free; run it under gpu_guard")
        try:
            p = subprocess.run([SA3_PYTHON, "-I", SA3_WORKER], input=json.dumps(req),
                               capture_output=True, text=True, timeout=duration * 3 + 300)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Stable Audio 3 timed out")
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
    if p.returncode != 0 or not lines:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError(f"Stable Audio 3 failed (rc {p.returncode}): {' | '.join(tail)[:400]}")
    result = json.loads(lines[-1][len("RESULT "):])
    result["seed"] = seed
    return result


def _render_music(engine="ace-step", **kw):
    if engine == "sa3":
        _render_sa3(**kw)
        return kw["out"]
    with _gpu:
        _make_room("music")
        fn = _music_v15 if ACE_STEP_VERSION == "1.5" else _music_v1
        return fn(**kw)


def sao():
    """Lazy-load Stable Audio Open. The service starts and serves /music without these weights."""
    global _sao
    if _sao is None:
        import torch
        from diffusers import StableAudioPipeline
        p = StableAudioPipeline.from_pretrained(SAO_MODEL, torch_dtype=torch.float16)
        _sao = p.to("cuda")
    return _sao


def _render_sfx(out, prompt, negative, duration_s, steps, seed):
    import soundfile as sf
    import torch
    with _gpu:
        _make_room("sfx")
        p = sao()
        gen = torch.Generator("cuda").manual_seed(seed)
        audio = p(prompt, negative_prompt=negative or None, num_inference_steps=steps,
                  audio_end_in_s=duration_s, num_waveforms_per_prompt=1, generator=gen).audios
        sr = int(p.vae.sampling_rate)
    sf.write(out, audio[0].T.float().cpu().numpy(), sr)
    return sr


class SongUnavailable(Exception):
    """/song can't run now (engine not installed, GPU too full): a 503, nothing was loaded."""


def _song_installed() -> dict:
    return {"venv": os.path.exists(HEARTMULA_PYTHON),
            "ckpt": all(os.path.exists(os.path.join(HEARTMULA_CKPT, p)) for p in _CKPT_PARTS)}


def _free_mib():
    """Free VRAM from nvidia-smi, so this process never opens a CUDA context just to ask."""
    try:
        # A systemd --user PATH often leaves out the WSL driver copy (#1426), as in gpu_guard.
        smi = shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"
        out = subprocess.run([smi, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15, check=True).stdout
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _song_mode(free):
    """'full' or 'lazy' for this much free VRAM, else SongUnavailable."""
    if free is None:
        raise SongUnavailable("can't read free VRAM (nvidia-smi failed); not starting HeartMuLa")
    if HEARTMULA_MODE != "lazy" and free >= SONG_FULL_MIB:
        return "full"
    if HEARTMULA_MODE != "full" and free >= SONG_LAZY_MIB:
        return "lazy"
    need = SONG_FULL_MIB if HEARTMULA_MODE == "full" else SONG_LAZY_MIB
    raise SongUnavailable(f"HeartMuLa needs {need} MiB free VRAM, {free} MiB free; run it under "
                          "gpu_guard (python3 -m media_worker.gpu_guard run --need-mib "
                          f"{SONG_GUARD_MIB} --stop nomad-stt -- ...)")


def _render_song(out, tags, lyrics, max_seconds, seed, cfg_scale, temperature, topk):
    """Run song_worker.py once in the HeartMuLa venv. Returns its RESULT dict plus the mode."""
    inst = _song_installed()
    if not all(inst.values()):
        missing = [k for k, ok in inst.items() if not ok]
        raise SongUnavailable(f"HeartMuLa not installed ({', '.join(missing)} missing under "
                              f"{HEARTMULA_ROOT}); run: {HERE}/install.sh heartmula")
    with _gpu:
        _make_room("song")
        mode = _song_mode(_free_mib())
        req = {"ckpt": HEARTMULA_CKPT, "out": out, "tags": tags, "lyrics": lyrics,
               "max_seconds": max_seconds, "seed": seed, "cfg_scale": cfg_scale,
               "temperature": temperature, "topk": topk, "lazy": mode == "lazy"}
        try:
            p = subprocess.run([HEARTMULA_PYTHON, "-I", SONG_WORKER], input=json.dumps(req),
                               capture_output=True, text=True, timeout=3 * max_seconds + 120)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"HeartMuLa timed out after {3 * max_seconds + 120:g} s")
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
    if p.returncode != 0 or not lines:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError(f"HeartMuLa failed (rc {p.returncode}): {' | '.join(tail)[:400]}")
    result = json.loads(lines[-1][len("RESULT "):])
    result["mode"] = mode
    return result


def _package_version():
    from importlib import metadata
    for dist in ("ace-step", "acestep"):
        try:
            return metadata.version(dist)
        except metadata.PackageNotFoundError:
            continue
    return None


@app.get("/health")
def health():
    return {"status": "ok", "backend": "ace-step", "ace_step_version": ACE_STEP_VERSION,
            "acestep_package": _package_version(),
            "model": ACESTEP15_CONFIG if ACE_STEP_VERSION == "1.5" else "ace-step-v1-3.5B",
            "bf16": BF16, "cpu_offload": CPU_OFFLOAD,
            "loaded": (_ace15 if ACE_STEP_VERSION == "1.5" else _pipe) is not None,
            "music": {"engines": list(MUSIC_ENGINES), "default": MUSIC_ENGINE,
                      "sa3": {"model": SA3_MODEL, "installed": os.path.exists(SA3_PYTHON),
                              "max_duration_s": SA3_MAX_S}},
            "sfx": {"model": SAO_MODEL, "loaded": _sao is not None, "max_duration_s": SAO_MAX_S},
            "song": {"model": SONG_MODEL, "installed": all(_song_installed().values()),
                     **_song_installed(), "mode": HEARTMULA_MODE, "full_mib": SONG_FULL_MIB,
                     "lazy_mib": SONG_LAZY_MIB, "guard_need_mib": SONG_GUARD_MIB, "max_seconds": MUSIC_MAX_S},
            "gpu_free_gb": _free_gb()}


@app.post("/music")
async def music(req: Request):
    body = await req.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt (style tags) required"}, status_code=400)
    lyrics = (body.get("lyrics") or "").strip()
    try:
        duration = float(body.get("duration", DEF_DURATION))
        steps = int(body["steps"]) if body.get("steps") is not None else None
        guidance = float(body["guidance_scale"]) if body.get("guidance_scale") is not None else None
        seed = int(body["seed"]) if body.get("seed") is not None else None
    except (TypeError, ValueError) as e:
        return JSONResponse({"error": f"bad parameter: {e}"}, status_code=400)
    engine = str(body.get("engine") or MUSIC_ENGINE).strip().lower()
    if engine not in MUSIC_ENGINES:
        return JSONResponse({"error": f"engine must be one of {', '.join(MUSIC_ENGINES)}"}, status_code=400)
    if engine == "sa3" and seed is None:
        seed = random.randrange(2**31)
    cap = SA3_MAX_S if engine == "sa3" else MUSIC_MAX_S
    if not 0 < duration <= cap:
        return JSONResponse({"error": f"duration must be in (0, {cap:g}]"}, status_code=400)

    out = os.path.join(OUT_DIR, f"music_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav")
    t0 = time.time()
    try:
        path = await run_in_threadpool(_admitted, req.headers.get(gpu_admission.LEASE_HEADER),
                                       _render_music, engine=engine, out=out, prompt=prompt,
                                       lyrics=lyrics, duration=duration, steps=steps,
                                       guidance=guidance, seed=seed)
    except gpu_admission.Busy as e:
        return _busy(e)
    except SongUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)[:500]}, status_code=500)
    dt = round(time.time() - t0, 1)
    model = f"sa3-{SA3_MODEL}" if engine == "sa3" else f"ace-step-{ACE_STEP_VERSION}"
    headers = {"X-Generation-Seconds": str(dt), "X-Audio-Path": path, "X-Model": model}
    if engine == "sa3":
        headers.update({"X-Sample-Rate": "44100", "X-Seed": str(seed)})
    return FileResponse(path, media_type="audio/wav", filename=os.path.basename(path), headers=headers)


@app.post("/sfx")
async def sfx(req: Request):
    body = await req.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt (sound description) required"}, status_code=400)
    try:
        duration_s = float(body.get("duration_s", SAO_DEF_S))
        steps = int(body.get("steps", SAO_STEPS))
        seed = int(body["seed"]) if body.get("seed") is not None else random.randrange(2**31)
    except (TypeError, ValueError) as e:
        return JSONResponse({"error": f"bad parameter: {e}"}, status_code=400)
    if not 0 < duration_s <= SAO_MAX_S:
        return JSONResponse({"error": f"duration_s must be in (0, {SAO_MAX_S:g}]"}, status_code=400)
    negative = (body.get("negative_prompt") or "Low quality.").strip()

    out = os.path.join(OUT_DIR, f"sfx_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav")
    t0 = time.time()
    try:
        sr = await run_in_threadpool(_admitted, req.headers.get(gpu_admission.LEASE_HEADER),
                                     _render_sfx, out, prompt, negative, duration_s, steps, seed)
    except gpu_admission.Busy as e:
        return _busy(e)
    except Exception as e:
        msg = str(e)[:500]
        if "gated" in msg.lower() or "401" in msg or "403" in msg:
            msg += (f" — accept the licence at huggingface.co/{SAO_MODEL} and log in "
                    "(`huggingface-cli login`, or HF_TOKEN in the service env)")
        return JSONResponse({"error": msg}, status_code=500)
    dt = round(time.time() - t0, 1)
    return FileResponse(out, media_type="audio/wav", filename=os.path.basename(out),
                        headers={"X-Generation-Seconds": str(dt), "X-Audio-Path": out,
                                 "X-Model": SAO_MODEL, "X-Seed": str(seed),
                                 "X-Duration-Seconds": f"{duration_s:g}",
                                 "X-Sample-Rate": str(sr)})


@app.post("/song")
async def song(req: Request):
    body = await req.json()
    tags = (body.get("tags") or "").strip()
    lyrics = (body.get("lyrics") or "").strip()
    if not tags:
        return JSONResponse({"error": "tags (style, e.g. 'latin pop, male vocal') required"}, status_code=400)
    if not lyrics:
        return JSONResponse({"error": "lyrics required (use [Verse] / [Chorus] tags)"}, status_code=400)
    if len(lyrics) > SONG_MAX_LYRICS or len(tags) > 1000:
        return JSONResponse({"error": f"lyrics over {SONG_MAX_LYRICS} or tags over 1000 characters"},
                            status_code=400)
    try:
        max_seconds = float(body.get("max_seconds", MUSIC_MAX_S))
        seed = int(body["seed"]) if body.get("seed") is not None else random.randrange(2**31)
        cfg_scale = float(body.get("cfg_scale", 3.0))
        temperature = float(body.get("temperature", 1.0))
        topk = int(body.get("topk", 50))
    except (TypeError, ValueError) as e:
        return JSONResponse({"error": f"bad parameter: {e}"}, status_code=400)
    if not 10 <= max_seconds <= MUSIC_MAX_S:
        return JSONResponse({"error": f"max_seconds must be in [10, {MUSIC_MAX_S:g}]"}, status_code=400)
    if not (0 < cfg_scale <= 10 and 0 < temperature <= 2 and 1 <= topk <= 500):
        return JSONResponse({"error": "cfg_scale in (0, 10], temperature in (0, 2], topk in [1, 500]"},
                            status_code=400)

    out = os.path.join(OUT_DIR, f"song_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav")
    t0 = time.time()
    try:
        r = await run_in_threadpool(_admitted, req.headers.get(gpu_admission.LEASE_HEADER),
                                    _render_song, out, tags, lyrics, max_seconds, seed,
                                    cfg_scale, temperature, topk)
    except gpu_admission.Busy as e:
        return _busy(e)
    except SongUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": str(e)[:500]}, status_code=500)
    dt = round(time.time() - t0, 1)
    return FileResponse(out, media_type="audio/wav", filename=os.path.basename(out),
                        headers={"X-Generation-Seconds": str(dt), "X-Audio-Path": out,
                                 "X-Model": SONG_MODEL, "X-Seed": str(seed),
                                 "X-Duration-Seconds": f"{r['seconds']:g}",
                                 "X-Sample-Rate": str(r["sample_rate"]), "X-Song-Mode": r["mode"]})


if __name__ == "__main__":
    import importlib.util   # dispatcher/bridge_bind.py, by path: loopback + NOMAD_BRIDGE_IP (#383)
    spec = importlib.util.spec_from_file_location("bridge_bind", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "dispatcher", "bridge_bind.py"))
    bridge_bind = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_bind)
    bridge_bind.serve_uvicorn(app, os.environ.get("NOMAD_AUDIO_HOST", "127.0.0.1"),   # loopback only (#821)
                              int(os.environ.get("NOMAD_AUDIO_PORT", "8220")))
