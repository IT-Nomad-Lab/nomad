"""NOMAD image bridge (MCP) — generate images from inside any project's Claude Code.

Lets Claude generate an image on the LOCAL GPU (ComfyUI) without leaving the editor, and saves it
straight into the project you're working in. Generation runs on your hardware (free, reversible —
the file is left uncommitted, like any Builder edit), so this direct path is LOCAL-ONLY. Cloud image
backends (OpenAI / Adobe Firefly) cost money and stay behind the approval gate — request those via
the NOMAD pipeline (cockpit/console or POST /capture) instead.

Tool:
  generate_image(prompt, subdir, filename) → renders locally, saves into <this project>/<subdir>/

It auto-detects the current project from the working directory, so the image lands in the right repo.
"""
import os

import requests
from mcp.server.fastmcp import FastMCP

# The v2 engine owns generation (provider chain + project save). Reach it on loopback or via the
# Windows-host gateway depending on the Docker/WSL setup — probe a few and reuse what works.
_CANDIDATES = [c for c in (
    os.environ.get("NOMAD_ENGINE_URL"),
    "http://127.0.0.1:8099",
    "http://host.docker.internal:8099",
    "http://172.21.128.1:8099",
) if c]
_base = None


def _engine():
    global _base
    if _base:
        return _base
    for b in _CANDIDATES:
        try:
            requests.get(b.rstrip("/") + "/health", timeout=2)
            _base = b.rstrip("/")
            return _base
        except Exception:
            continue
    _base = _CANDIDATES[0].rstrip("/")
    return _base


def _project_name() -> str:
    """The project NOMAD should save into = the basename of the dir Claude Code launched in."""
    return os.path.basename(os.getcwd().rstrip("/")) or "nomad"


mcp = FastMCP("nomad-image")


@mcp.tool()
def generate_image(prompt: str, subdir: str = "assets/nomad-images", filename: str = "") -> str:
    """Generate an image from `prompt` on the LOCAL GPU (ComfyUI) and save it into the CURRENT
    project's folder. `subdir` is the destination inside the repo (default 'assets/nomad-images';
    for Unity projects use e.g. 'Assets/Art/Generated'). `filename` is optional (auto-named .png).
    Local-only + free; the file is left uncommitted for you to review. Returns the saved path."""
    proj = _project_name()
    save_hint = f"{proj}/{subdir.strip('/')}"          # resolve_from_text + _parse_subdir parse this
    try:
        r = requests.post(f"{_engine()}/generate-image",
                          json={"prompt": prompt, "save_hint": save_hint, "filename": filename},
                          timeout=400)
        d = r.json()
    except Exception as e:
        return f"[image error: {str(e)[:240]}]"
    if not d.get("ok"):
        return f"[image generation failed: {d.get('error')}]"
    if d.get("project_file"):
        return f"Saved → {d['project_file']} (provider: {d.get('provider')})"
    # Generated but the project folder couldn't be resolved (is this repo registered with a Nomad.md?)
    return (f"Generated (provider: {d.get('provider')}) but NOT saved into the project "
            f"({d.get('project_save_error') or 'no project resolved — add a Nomad.md marker'}). "
            f"Available in the content library at {d.get('image_url')}.")


if __name__ == "__main__":
    mcp.run()
