# NOMAD image generation

NOMAD generates images through one gated tool — the v2 engine's `generate_image` (`v2/mcp_image.py`)
— backed by **three interchangeable providers**, tried in order (set by `NOMAD_IMAGE_PROVIDER`):

| Provider  | Where        | Cost            | Notes |
|-----------|--------------|-----------------|-------|
| `comfyui` | local (5090) | free            | Default first choice. SDXL or FLUX.1 schnell. |
| `openai`  | cloud        | per image       | `gpt-image-1` (uses `OPENAI_API_KEY`). |
| `firefly` | cloud        | Adobe credits   | Commercially-safe licensing (server-to-server OAuth). |

Generation is the **gated action** for the `ads` lane: it runs only after the human gate approves.
The result is saved under `v2/generated_images/` and filed to the `content` table as `/images/<file>`,
served by the engine and shown in the cockpit. The chain is **fail-soft** — if a provider is down or
unconfigured, the next one is tried; if all fail the run lands in `failed` (retryable).

Force a specific backend per call by passing `provider=` ("comfyui" | "openai" | "firefly").

## Install ComfyUI (local backend)

```bash
nomad-image/install_comfyui.sh        # clone + venv + PyTorch (cu128, Blackwell) + SDXL & FLUX models
nomad-image/install_service.sh        # optional: run ComfyUI on boot (systemd user unit, :8188)
# manual start instead of the service:
~/ComfyUI/venv/bin/python ~/ComfyUI/main.py --listen 0.0.0.0 --port 8188
```

ComfyUI listens on `0.0.0.0:8188` so the v2 engine **container** can reach it via
`host.docker.internal:8188`. WSL2 is NAT'd (not on the LAN) and ComfyUI has no auth — keep this box's
Windows firewall closed to inbound 8188.

### Switching the local model
`COMFYUI_MODEL=sdxl` (default, reliable) or `flux` (FLUX.1 schnell — higher quality, ~4 steps). Set it
in `.env` / the engine service env. Checkpoint filenames are overridable via `COMFYUI_SDXL_CKPT` /
`COMFYUI_FLUX_CKPT`. The FLUX graph uses `EmptySD3LatentImage` + cfg 1 / 4 steps; verify node names
against your ComfyUI version if a graph errors (`/history` will show the failing node).

## Apply the engine changes
The engine gained an `/images` mount + a `generated_images` volume + image env. Rebuild it:

```bash
docker compose up -d --build nomad-v2-engine
```
