"""NOMAD v2 — image generation MCP tool: `ads.generate_image` (multi-provider).

The gated Execute action for visual asks. Generation is the gated spend/work, so it runs ONLY
after the human gate approves. On success the image is persisted locally and a served path
(`/images/<file>`) is filed to the `content` table (sink), so it shows up like any content run.

Backends (selectable + ordered fallback via NOMAD_IMAGE_PROVIDER, default "comfyui,openai,firefly"):
  • comfyui — LOCAL on the RTX 5090 via ComfyUI's HTTP API (free, always-on, no per-image cost).
  • openai  — OpenAI Images API (gpt-image-1 / dall-e-3); costs per image.
  • firefly — Adobe Firefly server-to-server (commercially-safe licensing); spends Adobe credits.

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

# ── Adobe Firefly ──────────────────────────────────────────────────────────────────
IMS_TOKEN_URL = os.environ.get("ADOBE_IMS_TOKEN_URL", "https://ims-na1.adobelogin.com/ims/token/v3")
FIREFLY_URL = os.environ.get("FIREFLY_GENERATE_URL", "https://firefly-api.adobe.io/v3/images/generate")

# ── OpenAI images ──────────────────────────────────────────────────────────────────
OPENAI_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
OPENAI_IMAGE_SIZE = os.environ.get("OPENAI_IMAGE_SIZE", "1024x1024")

# ── Saving into a project folder (via the native dispatcher, which owns project resolution) ──
DISPATCH_URL = os.environ.get("NOMAD_DISPATCH_URL", "http://host.docker.internal:8090").rstrip("/")

# ── ComfyUI (local) ────────────────────────────────────────────────────────────────
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.docker.internal:8188").rstrip("/")
COMFYUI_MODEL = os.environ.get("COMFYUI_MODEL", "sdxl").lower()   # sdxl | flux | qwen
COMFYUI_SDXL_CKPT = os.environ.get("COMFYUI_SDXL_CKPT", "sd_xl_base_1.0.safetensors")
COMFYUI_FLUX_CKPT = os.environ.get("COMFYUI_FLUX_CKPT", "flux1-schnell-fp8.safetensors")
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


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _save_png(img_bytes: bytes, run_id: str) -> str:
    """Persist bytes under GENERATED_DIR and return the engine-served path (/images/<file>)."""
    fname = f"{(run_id or 'run')}_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
    with open(os.path.join(GENERATED_DIR, fname), "wb") as f:
        f.write(img_bytes)
    return f"/images/{fname}"


# ══ provider: ComfyUI (local) ═══════════════════════════════════════════════════════
def _comfy_graph(prompt: str) -> dict:
    """Build a text→image graph in ComfyUI API format for the configured local model."""
    seed = uuid.uuid4().int % (2 ** 32)
    if COMFYUI_MODEL == "qwen":
        # Qwen-Image (split components): UNETLoader + CLIPLoader(type qwen_image) + VAELoader.
        # Real CFG (negative prompt active), ~20 steps. Closest local model to gpt-image-1.
        return {
            "37": {"class_type": "UNETLoader",
                   "inputs": {"unet_name": COMFYUI_QWEN_UNET, "weight_dtype": "default"}},
            "38": {"class_type": "CLIPLoader",
                   "inputs": {"clip_name": COMFYUI_QWEN_CLIP, "type": "qwen_image", "device": "default"}},
            "39": {"class_type": "VAELoader", "inputs": {"vae_name": COMFYUI_QWEN_VAE}},
            "5": {"class_type": "EmptySD3LatentImage",
                  "inputs": {"width": COMFYUI_WIDTH, "height": COMFYUI_HEIGHT, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["38", 0]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": COMFYUI_NEGATIVE, "clip": ["38", 0]}},
            "3": {"class_type": "KSampler",
                  "inputs": {"seed": seed, "steps": COMFYUI_QWEN_STEPS, "cfg": COMFYUI_QWEN_CFG,
                             "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                             "model": ["37", 0], "positive": ["6", 0], "negative": ["7", 0],
                             "latent_image": ["5", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["39", 0]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "nomad_qwen", "images": ["8", 0]}},
        }
    if COMFYUI_MODEL == "flux":
        # FLUX.1 schnell (fp8 all-in-one checkpoint): distilled, ~4 steps, guidance off (cfg 1).
        return {
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": COMFYUI_FLUX_CKPT}},
            "5": {"class_type": "EmptySD3LatentImage",
                  "inputs": {"width": COMFYUI_WIDTH, "height": COMFYUI_HEIGHT, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
            "3": {"class_type": "KSampler",
                  "inputs": {"seed": seed, "steps": 4, "cfg": 1.0, "sampler_name": "euler",
                             "scheduler": "simple", "denoise": 1.0, "model": ["4", 0],
                             "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "nomad", "images": ["8", 0]}},
        }
    # SDXL base (standard KSampler graph).
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": COMFYUI_SDXL_CKPT}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": COMFYUI_WIDTH, "height": COMFYUI_HEIGHT, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": COMFYUI_NEGATIVE, "clip": ["4", 1]}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": 28, "cfg": 7.0, "sampler_name": "dpmpp_2m",
                         "scheduler": "karras", "denoise": 1.0, "model": ["4", 0],
                         "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "nomad", "images": ["8", 0]}},
    }


def _comfyui(prompt: str) -> bytes:
    """Queue a prompt on local ComfyUI, wait for it, and return the output PNG bytes."""
    client_id = uuid.uuid4().hex
    r = requests.post(f"{COMFYUI_URL}/prompt",
                      json={"prompt": _comfy_graph(prompt), "client_id": client_id}, timeout=30)
    r.raise_for_status()
    pid = r.json()["prompt_id"]
    deadline = time.time() + COMFYUI_TIMEOUT
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
    raise TimeoutError(f"ComfyUI did not finish within {COMFYUI_TIMEOUT}s")


# ══ provider: OpenAI ════════════════════════════════════════════════════════════════
def _openai(prompt: str) -> bytes:
    key = os.environ["OPENAI_API_KEY"]   # KeyError → "not configured", chain moves on
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
def _firefly_token() -> str:
    r = requests.post(IMS_TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": os.environ["ADOBE_CLIENT_ID"],
        "client_secret": os.environ["ADOBE_CLIENT_SECRET"],
        "scope": os.environ.get("ADOBE_FIREFLY_SCOPES", "openid,AdobeID,firefly_api,ff_apis"),
    }, timeout=30)
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
            "content_b64": base64.b64encode(img_bytes).decode()}, timeout=60)
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"dispatcher unreachable: {str(e)[:120]}"}


def generate_image_impl(prompt: str, run_id: str, provider: str = None, save_hint: str = None) -> dict:
    """Generate an image from `prompt`, persist it, and file the served path to `content`. Gated.

    Tries providers in order (NOMAD_IMAGE_PROVIDER, or a single `provider` override) and returns the
    first success. If `save_hint` names a known project, ALSO saves the image into that project's
    folder (via the dispatcher). Returns {ok, content_id, image_url, provider, project?, project_file?}
    or {ok:False, error} (all providers failed)."""
    chain = [p.strip().lower() for p in (provider or DEFAULT_CHAIN).split(",") if p.strip()]
    errors = []
    for name in chain:
        fn = _PROVIDERS.get(name)
        if not fn:
            errors.append(f"{name}: unknown provider")
            continue
        try:
            img_bytes = fn(prompt)
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
        except KeyError as e:
            errors.append(f"{name}: not configured (missing env {e})")
        except Exception as e:                      # network/API/parse — try the next provider
            errors.append(f"{name}: {str(e)[:160]}")
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
