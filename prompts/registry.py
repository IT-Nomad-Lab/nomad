"""NOMAD prompt registry — named, reusable prompt blocks any agent can call by name.

One flat namespace: each `<NAME>.md` in this directory is a prompt addressable as `<NAME>`.
`get_prompt("JDE_STYLE")` returns the text. Add a block by dropping a markdown file here; no code
change needed. Fail-open by design: an unknown or unreadable name returns "" so a missing prompt
degrades the output's style, never crashes the agent.

Consumers (crew, v2 specialists) locate this directory via NOMAD_PROMPTS_DIR (set in compose to the
mounted /app/prompts) or fall back to this file's own directory.
"""
import os

_DIR = os.environ.get("NOMAD_PROMPTS_DIR") or os.path.dirname(os.path.abspath(__file__))


def get_prompt(name: str, prompts_dir: str | None = None) -> str:
    """Return the named prompt block's text, or "" if it can't be read (fail-open)."""
    base = prompts_dir or _DIR
    try:
        with open(os.path.join(base, f"{name}.md"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def available(prompts_dir: str | None = None) -> list:
    """List registered prompt names (filenames without .md)."""
    base = prompts_dir or _DIR
    try:
        return sorted(f[:-3] for f in os.listdir(base)
                      if f.endswith(".md") and f[:-3].upper() != "README")
    except OSError:
        return []
