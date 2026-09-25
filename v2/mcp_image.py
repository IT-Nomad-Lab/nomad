"""NOMAD v2 — image generation MCP tool: `ads.generate_image` (multi-provider).

The gated Execute action for visual asks. Generation is the gated spend/work, so it runs ONLY
after the human gate approves. On success the image is persisted locally and a served path
(`/images/<file>`) is filed to the `content` table (sink), so it shows up like any content run.

Backends (selectable + ordered fallback via NOMAD_IMAGE_PROVIDER, default "comfyui,openai,firefly"):
  • comfyui — LOCAL on the RTX 5090 via ComfyUI's HTTP API (free, always-on, no per-image cost).
  • openai  — OpenAI Images API (gpt-image-1 / dall-e-3); costs per image.
  • firefly — Adobe Firefly server-to-server (commercially-safe licensing); spends Adobe credits.
              UNVERIFIED: no Adobe key has ever existed here, so it has never run against the live
              API and its endpoint paths are unconfirmed. It reports itself as not configured
              rather than failing like an outage, and it is last in the chain.

Every provider returns raw PNG bytes (URL-returning APIs are downloaded), which we save under
GENERATED_DIR (served by the engine at /images). The chain tries providers in order and returns the
first success; if all fail the run lands in 'failed' (retryable), never a hard crash.

Endpoint paths/payloads for the cloud providers version over time (CLAUDE.md open item) and are
env-overridable.
"""
import base64
import datetime
import json
import os
import time
import uuid

import requests
from mcp.server.fastmcp import FastMCP

from nocodb import NocoDB

_db = NocoDB()
mcp = FastMCP("nomad-image")

# ── where generated images are persisted + served from (engine mounts /images here) ──
GENERATED_DIR = os.environ.get("NOMAD_IMAGE_DIR",
                               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "generated_images"))
os.makedirs(GENERATED_DIR, exist_ok=True)

DEFAULT_CHAIN = os.environ.get("NOMAD_IMAGE_PROVIDER", "comfyui,openai,firefly")


class NotConfigured(Exception):
    """This provider was never set up, as opposed to set up and broken.

    The distinction is the whole point: 'all providers failed' is read by someone deciding whether
    something is on fire. A provider with no credentials is not an incident, it is a provider that
    was never turned on, and it should say so in those words. This used to be inferred from a bare
    KeyError, which also caught a KeyError raised while parsing a provider's RESPONSE and reported
    a live API returning an unexpected shape as 'not configured'.
    """


def _require_env(provider: str, *names: str):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise NotConfigured(f"set {' and '.join(missing)} in .env to enable {provider}")

# ── Adobe Firefly ──────────────────────────────────────────────────────────────────
IMS_TOKEN_URL = os.environ.get("ADOBE_IMS_TOKEN_URL", "https://ims-na1.adobelogin.com/ims/token/v3")
FIREFLY_URL = os.environ.get("FIREFLY_GENERATE_URL", "https://firefly-api.adobe.io/v3/images/generate")

# ── OpenAI images ──────────────────────────────────────────────────────────────────
OPENAI_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
OPENAI_IMAGE_SIZE = os.environ.get("OPENAI_IMAGE_SIZE", "1024x1024")

# ── Saving into a project folder (via the native dispatcher, which owns project resolution) ──
DISPATCH_URL = os.environ.get("NOMAD_DISPATCH_URL", "http://host.docker.internal:8090").rstrip("/")


def _dispatch_token():
    """Shared secret for the Builder dispatcher (auto-generated on the host at
    ~/.config/nomad/dispatch-token). The dispatcher refuses every route but /health without
    it. Candidates cover the container mounts and the host; '' means the call will 401."""
    env = os.environ.get("NOMAD_DISPATCH_TOKEN")
    if env:
        return env
    for cand in ("/term-secret/dispatch-token", "/host/.config/nomad/dispatch-token",
                 os.path.expanduser("~/.config/nomad/dispatch-token")):
        try:
            return open(cand).read().strip()
        except Exception:
            continue
    return ""


DISPATCH_TOKEN = _dispatch_token()

# ── ComfyUI (local) ────────────────────────────────────────────────────────────────
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.docker.internal:8188").rstrip("/")
COMFYUI_MODEL = os.environ.get("COMFYUI_MODEL", "sdxl").lower()   # sdxl | flux | flux2 | qwen
COMFYUI_SDXL_CKPT = os.environ.get("COMFYUI_SDXL_CKPT", "sd_xl_base_1.0.safetensors")
COMFYUI_FLUX_CKPT = os.environ.get("COMFYUI_FLUX_CKPT", "flux1-schnell-fp8.safetensors")
# FLUX.2 [dev] is split: DiT (diffusion_models) + Mistral-3 text encoder + VAE. Best quality +
# prompt adherence of the local models (closest local step toward gpt-image-1). Guidance-distilled,
# so real CFG stays 1.0 and prompt strength rides a FluxGuidance node. ⚠ non-commercial license
# (FLUX [dev]) — use Qwen (Apache-2.0) for anything commercial. Node/type names can drift by ComfyUI
# version, so the CLIP `type` and the latent node class are env-overridable.
COMFYUI_FLUX2_UNET = os.environ.get("COMFYUI_FLUX2_UNET", "flux2_dev_fp8mixed.safetensors")
# Text encoder: default to the **fp8** Mistral-3 (~17GB) not bf16 (~34GB). On a 24GB card the DiT
# already offloads ~13GB to CPU RAM; the bf16 encoder (34GB, CPU-resident) then blows past WSL's
# RAM and the OOM-killer takes ComfyUI down. fp8 halves it (footprint ~48GB → ~30GB). For even
# tighter RAM use mistral_3_small_flux2_fp4_mixed.safetensors (~12GB).
COMFYUI_FLUX2_CLIP = os.environ.get("COMFYUI_FLUX2_CLIP", "mistral_3_small_flux2_fp8.safetensors")
COMFYUI_FLUX2_VAE = os.environ.get("COMFYUI_FLUX2_VAE", "flux2-vae.safetensors")
COMFYUI_FLUX2_CLIP_TYPE = os.environ.get("COMFYUI_FLUX2_CLIP_TYPE", "flux2")
COMFYUI_FLUX2_LATENT = os.environ.get("COMFYUI_FLUX2_LATENT", "EmptyFlux2LatentImage")
COMFYUI_FLUX2_STEPS = int(os.environ.get("COMFYUI_FLUX2_STEPS", "20"))
COMFYUI_FLUX2_GUIDANCE = float(os.environ.get("COMFYUI_FLUX2_GUIDANCE", "4.0"))
# Qwen-Image is split: DiT (diffusion_models) + Qwen2.5-VL text encoder + VAE. Best prompt
# adherence + text rendering of the local models (closest to gpt-image-1), Apache-2.0.
COMFYUI_QWEN_UNET = os.environ.get("COMFYUI_QWEN_UNET", "qwen_image_fp8_e4m3fn.safetensors")
COMFYUI_QWEN_CLIP = os.environ.get("COMFYUI_QWEN_CLIP", "qwen_2.5_vl_7b_fp8_scaled.safetensors")
COMFYUI_QWEN_VAE = os.environ.get("COMFYUI_QWEN_VAE", "qwen_image_vae.safetensors")
COMFYUI_QWEN_STEPS = int(os.environ.get("COMFYUI_QWEN_STEPS", "20"))
COMFYUI_QWEN_CFG = float(os.environ.get("COMFYUI_QWEN_CFG", "2.5"))
COMFYUI_WIDTH = int(os.environ.get("COMFYUI_WIDTH", "1024"))
COMFYUI_HEIGHT = int(os.environ.get("COMFYUI_HEIGHT", "1024"))
COMFYUI_TIMEOUT = int(os.environ.get("COMFYUI_TIMEOUT", "300"))
COMFYUI_NEGATIVE = os.environ.get("COMFYUI_NEGATIVE",
                                  "lowres, blurry, watermark, text, deformed, extra limbs")
# SDXL style LoRA (nerijs/pixel-art-xl, OpenRAIL-M) — sprite/tile art SDXL base can't do on its own.
# It is NOT applied globally: a pixel LoRA on a photoreal prompt ruins it. It fires only when the
# prompt actually asks for pixel art (which is also how the LoRA's own trigger word works), so the
# default image path is byte-for-byte unchanged. Set _WHEN=always to force it on.
COMFYUI_SDXL_LORA = os.environ.get("COMFYUI_SDXL_LORA", "pixel-art-xl.safetensors")
COMFYUI_SDXL_LORA_STRENGTH = float(os.environ.get("COMFYUI_SDXL_LORA_STRENGTH", "1.0"))
COMFYUI_SDXL_LORA_WHEN = os.environ.get("COMFYUI_SDXL_LORA_WHEN",
                                        "pixel art,pixel-art,pixelart,sprite,8-bit,16-bit,retro game")
# The stock negative fights this LoRA: pixel art IS low-res and hard-edged, so asking against
# "lowres" softens exactly what we want. Swap in an anti-smoothing negative when the LoRA fires.
COMFYUI_SDXL_LORA_NEGATIVE = os.environ.get(
    "COMFYUI_SDXL_LORA_NEGATIVE",
    "blurry, soft focus, anti-aliased, smooth gradients, photorealistic, 3d render, watermark")

# Named styles (prompts/image_styles.json, mounted read-only into the engine): a style wraps the
# caller's scene prompt in a fixed prefix/suffix and pins model, size, steps and negative, so a
# brand look stays identical across posts. Read per call, so tuning it needs no rebuild.
_STYLE_FILES = [f for f in (
    os.environ.get("NOMAD_IMAGE_STYLES"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "image_styles.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts", "image_styles.json"),
) if f]


def load_style(name: str) -> dict:
    """The named style, or raise KeyError listing the ones that exist."""
    for path in _STYLE_FILES:
        if os.path.exists(path):
            with open(path) as f:
                styles = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
            if name in styles:
                return styles[name]
            raise KeyError(f"unknown image style {name!r} (have: {', '.join(sorted(styles))})")
    raise KeyError(f"no image_styles.json found (looked in {_STYLE_FILES})")


def apply_style(prompt: str, style: dict) -> str:
    """Scene prompt → full prompt: swap {tokens}, then wrap in the style's prefix/suffix."""
    for token, text in (style.get("tokens") or {}).items():
        prompt = prompt.replace(token, text)
    return f"{style.get('prefix', '')}{prompt}{style.get('suffix', '')}"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _save_png(img_bytes: bytes, run_id: str) -> str:
    """Persist bytes under GENERATED_DIR and return the engine-served path (/images/<file>)."""
    fname = f"{(run_id or 'run')}_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
    with open(os.path.join(GENERATED_DIR, fname), "wb") as f:
        f.write(img_bytes)
    return f"/images/{fname}"


# ══ provider: ComfyUI (local) ═══════════════════════════════════════════════════════
def _sdxl_lora(prompt: str) -> str:
    """The SDXL style LoRA to apply to this prompt, or "" for none (the default path)."""
    if not COMFYUI_SDXL_LORA:
        return ""
    when = COMFYUI_SDXL_LORA_WHEN.strip().lower()
    if when == "always":
        return COMFYUI_SDXL_LORA
    p = prompt.lower()
    return COMFYUI_SDXL_LORA if any(k.strip() and k.strip() in p for k in when.split(",")) else ""


def _comfy_graph(prompt: str, style: dict = None) -> dict:
    """Build a text→image graph in ComfyUI API format for the configured local model, or for the
    model/size/steps/negative a named style pins."""
    st = style or {}
    seed = uuid.uuid4().int % (2 ** 32)
    model = st.get("model", COMFYUI_MODEL).lower()
    width, height = st.get("width", COMFYUI_WIDTH), st.get("height", COMFYUI_HEIGHT)
    if model == "qwen":
        # Qwen-Image (split components): UNETLoader + CLIPLoader(type qwen_image) + VAELoader.
        # Real CFG (negative prompt active), ~20 steps. Closest local model to gpt-image-1.
        return {
            "37": {"class_type": "UNETLoader",
                   "inputs": {"unet_name": COMFYUI_QWEN_UNET, "weight_dtype": "default"}},
            "38": {"class_type": "CLIPLoader",
                   "inputs": {"clip_name": COMFYUI_QWEN_CLIP, "type": "qwen_image", "device": "default"}},
            "39": {"class_type": "VAELoader", "inputs": {"vae_name": COMFYUI_QWEN_VAE}},
            "5": {"class_type": "EmptySD3LatentImage",
                  "inputs": {"width": width, "height": height, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["38", 0]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": st.get("negative", COMFYUI_NEGATIVE), "clip": ["38", 0]}},
            "3": {"class_type": "KSampler",
                  "inputs": {"seed": seed, "steps": st.get("steps", COMFYUI_QWEN_STEPS),
                             "cfg": st.get("cfg", COMFYUI_QWEN_CFG),
                             "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                             "model": ["37", 0], "positive": ["6", 0], "negative": ["7", 0],
                             "latent_image": ["5", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["39", 0]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "nomad_qwen", "images": ["8", 0]}},
        }
    if model == "flux2":
        # FLUX.2 [dev] (split: DiT + Mistral-3 text encoder + VAE). Guidance-distilled → real CFG
        # off (cfg 1.0); prompt strength via FluxGuidance. ~20 steps, euler/simple. Node class
        # names (CLIP type, latent) are env-overridable since ComfyUI versions can rename them.
        return {
            "37": {"class_type": "UNETLoader",
                   "inputs": {"unet_name": COMFYUI_FLUX2_UNET, "weight_dtype": "default"}},
            "38": {"class_type": "CLIPLoader",
                   "inputs": {"clip_name": COMFYUI_FLUX2_CLIP, "type": COMFYUI_FLUX2_CLIP_TYPE,
                              "device": "default"}},
            "39": {"class_type": "VAELoader", "inputs": {"vae_name": COMFYUI_FLUX2_VAE}},
            "5": {"class_type": COMFYUI_FLUX2_LATENT,
                  "inputs": {"width": width, "height": height, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["38", 0]}},
            "10": {"class_type": "FluxGuidance",
                   "inputs": {"conditioning": ["6", 0], "guidance": COMFYUI_FLUX2_GUIDANCE}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["38", 0]}},
            "3": {"class_type": "KSampler",
                  "inputs": {"seed": seed, "steps": COMFYUI_FLUX2_STEPS, "cfg": 1.0,
                             "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                             "model": ["37", 0], "positive": ["10", 0], "negative": ["7", 0],
                             "latent_image": ["5", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["39", 0]}},
            "9": {"class_type": "SaveImage",
                  "inputs": {"filename_prefix": "nomad_flux2", "images": ["8", 0]}},
        }
    if model == "flux":
        # FLUX.1 schnell (fp8 all-in-one checkpoint): distilled, ~4 steps, guidance off (cfg 1).
        return {
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": COMFYUI_FLUX_CKPT}},
            "5": {"class_type": "EmptySD3LatentImage",
                  "inputs": {"width": width, "height": height, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
            "3": {"class_type": "KSampler",
                  "inputs": {"seed": seed, "steps": 4, "cfg": 1.0, "sampler_name": "euler",
                             "scheduler": "simple", "denoise": 1.0, "model": ["4", 0],
                             "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "nomad", "images": ["8", 0]}},
        }
    # SDXL base (standard KSampler graph), optionally through a style LoRA.
    lora = _sdxl_lora(prompt)
    src = ["10", 0] if lora else ["4", 0]        # model  ← LoraLoader when active
    clip = ["10", 1] if lora else ["4", 1]       # clip   ← same (LoRA patches both)
    negative = COMFYUI_SDXL_LORA_NEGATIVE if lora else COMFYUI_NEGATIVE
    graph = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": COMFYUI_SDXL_CKPT}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": clip}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": clip}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": 28, "cfg": 7.0, "sampler_name": "dpmpp_2m",
                         "scheduler": "karras", "denoise": 1.0, "model": src,
                         "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "nomad", "images": ["8", 0]}},
    }
    if lora:
        graph["10"] = {"class_type": "LoraLoader",
                       "inputs": {"lora_name": lora,
                                  "strength_model": COMFYUI_SDXL_LORA_STRENGTH,
                                  "strength_clip": COMFYUI_SDXL_LORA_STRENGTH,
                                  "model": ["4", 0], "clip": ["4", 1]}}
    return graph


def _comfyui(prompt: str, style: dict = None) -> bytes:
    """Queue a prompt on local ComfyUI, wait for it, and return the output PNG bytes."""
    client_id = uuid.uuid4().hex
    r = requests.post(f"{COMFYUI_URL}/prompt",
                      json={"prompt": _comfy_graph(prompt, style), "client_id": client_id}, timeout=30)
    r.raise_for_status()
    pid = r.json()["prompt_id"]
    limit = (style or {}).get("timeout", COMFYUI_TIMEOUT)
    deadline = time.time() + limit
    while time.time() < deadline:
        h = requests.get(f"{COMFYUI_URL}/history/{pid}", timeout=30).json()
        if pid in h:
            outputs = h[pid].get("outputs", {})
            for node in outputs.values():
                for img in node.get("images", []):
                    v = requests.get(f"{COMFYUI_URL}/view",
                                     params={"filename": img["filename"],
                                             "subfolder": img.get("subfolder", ""),
                                             "type": img.get("type", "output")}, timeout=60)
                    v.raise_for_status()
                    return v.content
            raise RuntimeError(f"ComfyUI finished with no image output: {json.dumps(outputs)[:200]}")
        time.sleep(1.0)
    raise TimeoutError(f"ComfyUI did not finish within {limit}s")


# ══ provider: OpenAI ════════════════════════════════════════════════════════════════
def _openai(prompt: str) -> bytes:
    _require_env("openai", "OPENAI_API_KEY")   # NotConfigured → chain moves on, says why
    key = os.environ["OPENAI_API_KEY"]
    r = requests.post(f"{OPENAI_BASE}/images/generations",
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json={"model": OPENAI_IMAGE_MODEL, "prompt": prompt[:4000],
                            "n": 1, "size": OPENAI_IMAGE_SIZE}, timeout=180)
    r.raise_for_status()
    item = r.json()["data"][0]
    if item.get("b64_json"):                       # gpt-image-1 returns base64
        return base64.b64decode(item["b64_json"])
    return requests.get(item["url"], timeout=120).content   # dall-e-3 returns a URL


# ══ provider: Adobe Firefly ═════════════════════════════════════════════════════════
# No Adobe credentials exist here, so this provider has never run and its endpoint paths have
# never been confirmed against the live API. It stays in the file deliberately, waiting for a key.
# What it must not do is fail in a way that reads like an outage: the chain reports every provider
# error together, and "missing ADOBE_CLIENT_ID" and "Firefly returned 500" need different answers.
def _firefly_token() -> str:
    _require_env("firefly", "ADOBE_CLIENT_ID", "ADOBE_CLIENT_SECRET")
    r = requests.post(IMS_TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": os.environ["ADOBE_CLIENT_ID"],
        "client_secret": os.environ["ADOBE_CLIENT_SECRET"],
        "scope": os.environ.get("ADOBE_FIREFLY_SCOPES", "openid,AdobeID,firefly_api,ff_apis"),
    }, timeout=30)
    if r.status_code in (401, 403):
        raise RuntimeError(f"Adobe rejected the credentials ({r.status_code}) — the key is set but "
                           f"wrong, or lacks the firefly_api scope")
    r.raise_for_status()
    return r.json()["access_token"]


def _firefly_extract_url(data):
    for path in (("outputs", 0, "image", "url"), ("outputs", 0, "image", "presignedUrl"),
                 ("images", 0, "url"), ("result", "outputs", 0, "image", "url")):
        try:
            d = data
            for k in path:
                d = d[k]
            if isinstance(d, str):
                return d
        except (KeyError, IndexError, TypeError):
            continue
    return None


def _firefly(prompt: str) -> bytes:
    token = _firefly_token()
    r = requests.post(FIREFLY_URL, headers={
        "Authorization": f"Bearer {token}",
        "x-api-key": os.environ["ADOBE_CLIENT_ID"],
        "Content-Type": "application/json",
    }, json={"prompt": prompt[:1000], "numVariations": 1}, timeout=120)
    r.raise_for_status()
    url = _firefly_extract_url(r.json())
    if not url:
        raise RuntimeError(f"no image URL in Firefly response: {str(r.json())[:160]}")
    return requests.get(url, timeout=120).content


_PROVIDERS = {"comfyui": _comfyui, "openai": _openai, "firefly": _firefly}


def _save_to_project(img_bytes: bytes, hint: str, filename: str) -> dict:
    """Ask the dispatcher to save the image into the project NAMED in `hint`. Fail-soft:
    returns {ok:False, no_project:True} when no project was named (skip silently)."""
    try:
        r = requests.post(f"{DISPATCH_URL}/save-image", json={
            "text": hint, "filename": filename,
            "content_b64": base64.b64encode(img_bytes).decode()},
            headers={"X-Nomad-Token": DISPATCH_TOKEN}, timeout=60)
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"dispatcher unreachable: {str(e)[:120]}"}


def generate_image_impl(prompt: str, run_id: str, provider: str = None, save_hint: str = None,
                        style: str = None) -> dict:
    """Generate an image from `prompt`, persist it, and file the served path to `content`. Gated.

    Tries providers in order (NOMAD_IMAGE_PROVIDER, or a single `provider` override) and returns the
    first success. If `save_hint` names a known project, ALSO saves the image into that project's
    folder (via the dispatcher). Returns {ok, content_id, image_url, provider, project?, project_file?}
    or {ok:False, error} (all providers failed). `style` names a preset in prompts/image_styles.json:
    `prompt` is then just the scene, and the style supplies the look, model and size."""
    st = None
    if style:
        try:
            st = load_style(style)
        except KeyError as e:
            return {"ok": False, "error": str(e).strip("'\"")}
        prompt = apply_style(prompt, st)
    chain = [p.strip().lower() for p in (provider or DEFAULT_CHAIN).split(",") if p.strip()]
    errors = []
    for name in chain:
        fn = _PROVIDERS.get(name)
        if not fn:
            errors.append(f"{name}: unknown provider")
            continue
        try:
            img_bytes = fn(prompt, st) if name == "comfyui" else fn(prompt)
            ref = _save_png(img_bytes, run_id)
            row = _db.create("content", {"topic": prompt[:80], "content": ref, "run_id": run_id,
                                         "created_at": _now()})
            res = {"ok": True, "content_id": row["Id"], "image_url": ref, "provider": name,
                   "summary": f"image ({name}) → {ref}"}
            if save_hint:                            # caller asked to drop it into a project folder
                saved = _save_to_project(img_bytes, save_hint, os.path.basename(ref))
                if saved.get("ok"):
                    res["project"] = saved.get("project")
                    res["project_file"] = saved.get("path")
                    res["summary"] += f" + saved to {saved.get('project')}:{saved.get('rel')}"
                elif not saved.get("no_project"):    # a project was named but the save failed
                    res["project_save_error"] = saved.get("error")
                    res["summary"] += f" (project save failed: {saved.get('error')})"
            return res
        except NotConfigured as e:                  # never turned on, not broken — say which
            errors.append(f"{name}: not configured ({e})")
        except Exception as e:                      # network/API/parse — try the next provider
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:160]}")
    return {"ok": False, "error": "all image providers failed — " + "; ".join(errors)}


@mcp.tool()
def generate_image(prompt: str, run_id: str, provider: str = None, save_hint: str = None) -> dict:
    """Generate an image from `prompt` (runs only after the human gate approves). Backends, in order:
    ComfyUI (local/free) → OpenAI (gpt-image-1) → Adobe Firefly. Pass `provider` to force one
    ("comfyui"|"openai"|"firefly"). If `save_hint` names a known project, the image is also saved
    into that project's folder. Persists the image and returns {ok, content_id, image_url, project?}."""
    return generate_image_impl(prompt, run_id, provider, save_hint)


if __name__ == "__main__":
    mcp.run()
