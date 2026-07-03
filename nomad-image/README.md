# NOMAD image generation

NOMAD generates images through one gated tool — the v2 engine's `generate_image` (`v2/mcp_image.py`)
— backed by **three interchangeable providers**, tried in order (set by `NOMAD_IMAGE_PROVIDER`):

| Provider  | Where        | Cost            | Notes |
|-----------|--------------|-----------------|-------|
| `comfyui` | local (5090) | free            | Default first choice. SDXL · FLUX.1 schnell · **FLUX.2 [dev]** · Qwen-Image. |
| `openai`  | cloud        | per image       | `gpt-image-1` (uses `OPENAI_API_KEY`). |
| `firefly` | cloud        | Adobe credits   | Commercially-safe licensing (server-to-server OAuth). |

Generation is the **gated action** for the `ads` lane: it runs only after the human gate approves.
The result is saved under `v2/generated_images/` and filed to the `content` table as `/images/<file>`,
served by the engine and shown in the cockpit. The chain is **fail-soft** — if a provider is down or
unconfigured, the next one is tried; if all fail the run lands in `failed` (retryable).

Force a specific backend per call by passing `provider=` ("comfyui" | "openai" | "firefly").

## Install ComfyUI (local backend)

```bash
nomad-image/install_comfyui.sh        # clone + venv + PyTorch (cu128, Blackwell) + SDXL & FLUX.1 models
nomad-image/install_flux2.sh          # add FLUX.2 [dev]  (~35GB fp8-mixed: DiT + Mistral-3 encoder + VAE)
nomad-image/install_qwen.sh           # add Qwen-Image    (Apache-2.0, best in-image text)
nomad-image/install_service.sh        # optional: run ComfyUI on boot (systemd user unit, :8188)
# manual start instead of the service:
~/ComfyUI/venv/bin/python ~/ComfyUI/main.py --listen 0.0.0.0 --port 8188
```

ComfyUI listens on `0.0.0.0:8188` so the v2 engine **container** can reach it via
`host.docker.internal:8188`. WSL2 is NAT'd (not on the LAN) and ComfyUI has no auth — keep this box's
Windows firewall closed to inbound 8188.

### Switching the local model
Set `COMFYUI_MODEL` in `.env` / the engine service env:

| Value | Model | Best for | License |
|-------|-------|----------|---------|
| `sdxl` | SDXL base | reliable default, fast | open |
| `flux` | FLUX.1 schnell | higher quality, ~4 steps | Apache-2.0 |
| `flux2` | **FLUX.2 [dev]** | **best quality + prompt adherence** (closest local to gpt-image-1) | ⚠ **non-commercial** |
| `qwen` | Qwen-Image | best in-image **text**; commercial-safe | Apache-2.0 |

> **Commercial use (selling generated assets):** prefer `qwen` (Apache-2.0). FLUX `[dev]` models
> are non-commercial — great for quality, but don't sell their output without a commercial license.

Model filenames + params are env-overridable: `COMFYUI_SDXL_CKPT`, `COMFYUI_FLUX_CKPT`, the
`COMFYUI_QWEN_*` set, and the `COMFYUI_FLUX2_*` set (`_UNET` / `_CLIP` / `_VAE` / `_STEPS` /
`_GUIDANCE`, plus `_CLIP_TYPE` and `_LATENT` for the two node-class names that can drift between
ComfyUI versions). FLUX.2 is guidance-distilled (cfg 1.0 + a `FluxGuidance` node, ~20 steps). If a
graph errors, `/history` shows the failing node — usually a renamed node class, fixable via those
two env vars. FLUX.2 needs a **recent ComfyUI** (native FLUX.2 nodes).

## Apply the engine changes
The engine gained an `/images` mount + a `generated_images` volume + image env. Rebuild it:

```bash
docker compose up -d --build nomad-v2-engine
```
