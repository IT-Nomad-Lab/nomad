"""NOMAD Codex image bridge (MCP) — generate an image with OpenAI Codex, on request.

The LOCAL path (`nomad-image` → ComfyUI/SDXL) stays the default: free, on your own GPU, no quota.
This tool is the one you ask for BY NAME when prompt adherence matters more than cost — Codex
renders through its native `image_generation` feature on the ChatGPT subscription, and on the same
brief it lands details SDXL drops (the actual rack, the single amber LED, the rain on the window).

Deliberately NOT in the `generate_image` provider chain. It spends subscription quota, so it runs
when you ask for it, never as a silent fallback.

Tool:
  codex_image(prompt, subdir, filename) → renders via Codex, saves into <this project>/<subdir>/

Why this can't go through `codex-bridge` (:8089): generating an image WRITES a file, and the bridge
runs `-s read-only` in an empty workdir on purpose so it stays a model endpoint. This shells out
separately with a writable scratch dir instead of loosening that guarantee.
"""
import glob
import os
import re
import shutil
import subprocess
import tempfile
import time

from mcp.server.fastmcp import FastMCP

CODEX = shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")
TIMEOUT = int(os.environ.get("NOMAD_CODEX_IMAGE_TIMEOUT", "600"))
# Where the CLI drops what it renders before anything copies it out.
CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
GENERATED = os.path.join(CODEX_HOME, "generated_images")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")

mcp = FastMCP("nomad-codex-image")


def _project_dir() -> str:
    """Save into the repo Claude Code was launched in — same rule as `nomad-image`."""
    return os.getcwd().rstrip("/") or os.path.expanduser("~/nomad")


def _safe_name(filename: str, prompt: str) -> str:
    if filename.strip():
        name = os.path.basename(filename.strip())
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40] or "codex-image"
        name = f"{slug}-{int(time.time())}.png"
    return name if name.lower().endswith(IMAGE_EXT) else name + ".png"


def _newest_image(root: str, since: float) -> str:
    """Newest image under `root` written after `since` (0 = ignore mtime)."""
    best, best_m = "", since
    for path in glob.iglob(os.path.join(root, "**", "*"), recursive=True):
        if not path.lower().endswith(IMAGE_EXT) or not os.path.isfile(path):
            continue
        m = os.path.getmtime(path)
        if m >= best_m:
            best, best_m = path, m
    return best


@mcp.tool()
def codex_image(prompt: str, subdir: str = "assets/nomad-images", filename: str = "") -> str:
    """Generate an image from `prompt` using OPENAI CODEX (its native image generation, on the
    ChatGPT subscription) and save it into the CURRENT project's folder. Ask for this by name when
    you want Codex specifically — prompt adherence beats the local SDXL path, but it spends
    subscription quota, so `generate_image` (local, free) stays the default. `subdir` is the
    destination inside the repo. `filename` is optional (auto-named .png). Returns the saved path."""
    if not os.path.exists(CODEX):
        return f"[codex not found at {CODEX} — is @openai/codex installed?]"

    name = _safe_name(filename, prompt)
    dest_dir = os.path.join(_project_dir(), subdir.strip("/"))
    started = time.time()
    scratch = tempfile.mkdtemp(prefix="nomad-codex-img-")
    try:
        instruction = (
            f"Use your image generation tool to create an image and save it in the current "
            f"directory as {name}. Do not write any other files. Reply with only the filename.\n\n"
            f"Image: {prompt}"
        )
        try:
            proc = subprocess.run(
                [CODEX, "exec", "--skip-git-repo-check", "--ephemeral",
                 "--ignore-user-config", "-s", "workspace-write", "-C", scratch,
                 "--color", "never", instruction],
                capture_output=True, text=True, timeout=TIMEOUT, cwd=scratch,
                # A key here would switch this to the metered Images API. Force the subscription.
                env={**os.environ, "OPENAI_API_KEY": ""},
            )
        except subprocess.TimeoutExpired:
            return f"[codex image timed out after {TIMEOUT}s]"

        # Prefer what landed in the scratch dir; fall back to the CLI's own output folder, since
        # the model sometimes reports success without copying the file out.
        src = _newest_image(scratch, 0)
        if not src and os.path.isdir(GENERATED):
            src = _newest_image(GENERATED, started)
        if not src:
            tail = (proc.stderr or proc.stdout or "")[-400:]
            return f"[codex produced no image (rc={proc.returncode}): {tail}]"

        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        shutil.copyfile(src, dest)
        size = os.path.getsize(dest)
        return (f"Saved → {dest} ({size // 1024} KB, provider: codex, "
                f"{int(time.time() - started)}s)")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    mcp.run()
