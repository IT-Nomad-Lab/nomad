"""NOMAD audio bridge (MCP) — generate music from inside any project's Claude Code.

Lets Claude write a song or a background track on the LOCAL GPU (the nomad-audio service, ACE-Step
v1, Apache-2.0) and saves the WAV straight into the project you're working in. Free, no quota,
nothing leaves the box — the file is left uncommitted for you to review, like any Builder edit.

Tool:
  generate_music(prompt, lyrics, duration, seed, subdir, filename) → renders locally, saves into
  <this project>/<subdir>/

The service writes its own copy to ~/nomad/nomad-audio/generated_audio/; this tool copies the
result into the repo Claude Code was launched in. It never overwrites: an existing file gets a
numbered suffix instead, so a game's live `bgm.wav` can't be clobbered by accident.
"""
import os
import re
import time

import requests
from mcp.server.fastmcp import FastMCP

# nomad-audio is host-native (not a container). Probe loopback first, then the Windows-host
# gateway, and reuse whichever answers.
_CANDIDATES = [c for c in (
    os.environ.get("NOMAD_AUDIO_URL"),
    "http://127.0.0.1:8220",
    "http://host.docker.internal:8220",
) if c]
_base = None
TIMEOUT = int(os.environ.get("NOMAD_AUDIO_TIMEOUT", "900"))   # first call loads the model
MAX_DURATION = 240.0

mcp = FastMCP("nomad-audio")


def _service():
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
    return None


def _project_dir() -> str:
    """Save into the repo Claude Code was launched in — same rule as `nomad-codex-image`."""
    return os.getcwd().rstrip("/") or os.path.expanduser("~/nomad")


def _safe_name(filename: str, prompt: str) -> str:
    if filename.strip():
        name = os.path.basename(filename.strip())
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40] or "nomad-music"
        name = f"{slug}-{int(time.time())}"
    return name if name.lower().endswith(".wav") else os.path.splitext(name)[0] + ".wav"


def _free_path(path: str) -> str:
    """`path` if unused, else `name-2.wav`, `name-3.wav`, … Never overwrite."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


@mcp.tool()
def generate_music(prompt: str, lyrics: str = "", duration: float = 30, seed: int = -1,
                   subdir: str = "assets/nomad-audio", filename: str = "") -> str:
    """Generate music on the LOCAL GPU (ACE-Step via nomad-audio) and save a 48 kHz stereo WAV into
    the CURRENT project's folder.

    `prompt` = comma-separated style tags: genre, instruments, mood, vocal type, bpm
    (e.g. "calm fantasy, celtic harp, soft flute, warm pads, 80 bpm").
    `lyrics` = optional, with [verse] / [chorus] / [bridge] / [outro] sections. Leave empty for an
    instrumental. `duration` = seconds (max 240). `seed` = fix it (>= 0) to reproduce a take; -1 is
    random. `subdir` = destination inside the repo (Unity: e.g. 'Assets/Resources/Audio/BGM').
    `filename` = optional (auto-named .wav); an existing file is never overwritten.

    Limits: text-only (it can't cover or remix an existing song's melody), bpm is a hint, output
    doesn't loop seamlessly, and it isn't for sub-second SFX. Returns the saved path."""
    if not prompt.strip():
        return "[music error: prompt (style tags) is required]"
    base = _service()
    if not base:
        return ("[nomad-audio is not reachable on :8220 — start it with "
                "`systemctl --user start nomad-audio`]")

    body = {
        "prompt": prompt.strip(),
        "lyrics": lyrics.strip() or "[instrumental]",
        "duration": max(1.0, min(float(duration), MAX_DURATION)),
    }
    if seed is not None and seed >= 0:
        body["seed"] = int(seed)

    started = time.time()
    try:
        r = requests.post(f"{base}/music", json=body, timeout=TIMEOUT)
    except requests.Timeout:
        return f"[music generation timed out after {TIMEOUT}s]"
    except Exception as e:
        return f"[music error: {str(e)[:240]}]"
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("audio/"):
        try:
            err = r.json().get("error")
        except Exception:
            err = r.text[:240]
        return f"[music generation failed ({r.status_code}): {err}]"

    dest_dir = os.path.join(_project_dir(), subdir.strip("/"))
    os.makedirs(dest_dir, exist_ok=True)
    dest = _free_path(os.path.join(dest_dir, _safe_name(filename, prompt)))
    with open(dest, "wb") as f:
        f.write(r.content)
    gen = r.headers.get("x-generation-seconds", f"{time.time() - started:.1f}")
    seed_note = f", seed {body['seed']}" if "seed" in body else ""
    return (f"Saved → {dest} ({len(r.content) // 1024} KB, {body['duration']:.0f}s audio, "
            f"generated in {gen}s{seed_note}, provider: ace-step local)")


if __name__ == "__main__":
    mcp.run()
