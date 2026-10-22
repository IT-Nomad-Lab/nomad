"""NOMAD detector bridge (MCP) — check text for AI tells without leaving the editor.

Wraps the local nomad-detect service so Claude Code (and any agent that speaks MCP) can run its
own drafts through the detector before handing them over. This is the enforcement arm of the
JDE_STYLE detection pass: write, check, rewrite the findings, check again.

Tools:
  detect_ai(text)   full check — local Binoculars score (GPU) + style linter + verdict
  lint_style(text)  style only — instant, no model, returns the exact lines to fix
"""
import json
import os
from typing import Annotated

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

_CANDIDATES = [c for c in (
    os.environ.get("NOMAD_DETECT_URL"),
    "http://127.0.0.1:8214",
    "http://host.docker.internal:8214",
    "http://172.21.128.1:8214",
) if c]
_base = None


def _detect():
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


def _render(d: dict) -> str:
    sty, bino = d.get("style", {}), d.get("binoculars", {})
    lines = [f"VERDICT: {str(d.get('verdict', '?')).upper()} — {d.get('guidance', '')}"]
    if bino.get("ok"):
        t = bino.get("thresholds", {})
        lines.append(f"statistical: {bino['score']} [{bino['band']}] vs threshold "
                     f"{t.get('accuracy')} ({'calibrated' if t.get('calibrated') else 'defaults'})")
    elif bino:
        lines.append(f"statistical: unavailable ({bino.get('error')})")
    if sty:
        m = sty["metrics"]
        lines.append(f"style: {sty['score']}/100 [{sty['band']}] · burstiness {m['burstiness']} · "
                     f"{m['concrete_per_100w']} specifics/100w · {sty['summary']['total']} findings")
    if d.get("caveat"):
        lines.append(f"caveat: {d['caveat']}")
    for f in (d.get("rewrite_hints") or sty.get("findings", [])[:10]):
        lines.append(f"  L{f['line']} [{f.get('severity', '')}] {f['rule']}: “{f['excerpt'][:70]}”")
        lines.append(f"     → {f['fix']}")
    return "\n".join(lines)


mcp = FastMCP("nomad-detect")


# Both tools only read: the detector scores the text and keeps nothing. detect_ai is open-world
# because external=True sends the text to paid third-party checkers.
@mcp.tool(title="Detect AI writing",
          annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def detect_ai(text: Annotated[str, Field(description="The draft to check, plain text or Markdown.")],
              external: Annotated[bool, Field(description="Also send the text to the configured paid "
                                              "checkers (GPTZero, Originality, Sapling). Off by default: "
                                              "the check then stays on this machine.")] = False) -> str:
    """Score a piece of writing for AI tells. Runs a local statistical detector (Binoculars, two
    LMs on the GPU, about a second) plus the JDE_STYLE tell linter, and returns a verdict
    (pass / revise / fail), the scores, and the exact lines to rewrite with a fix for each.
    With external=True the text also goes to any configured paid checker and their scores are
    appended. For a quick style-only pass with no GPU, use lint_style."""
    try:
        r = requests.post(f"{_detect()}/detect", json={"text": text, "external": external},
                          timeout=300)
        d = r.json()
        if not d.get("ok"):
            return f"[detect failed: {d.get('error')}]"
        out = _render(d)
        if d.get("external"):
            out += "\nexternal: " + json.dumps(d["external"])
        return out
    except Exception as e:
        return f"[detector unreachable: {str(e)[:200]}]"


@mcp.tool(title="Lint style",
          annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
def lint_style(text: Annotated[str, Field(description="The draft to check.")]) -> str:
    """Style-only check (instant, no GPU): flags the constructions and rhythm that mark text as
    machine-written and violate prompts/JDE_STYLE.md, with a fix for each. No statistical score;
    for the full verdict use detect_ai."""
    try:
        r = requests.post(f"{_detect()}/lint", json={"text": text}, timeout=30)
        d = r.json()
        if not d.get("ok"):
            return f"[lint failed: {d.get('error')}]"
        return _render({"verdict": d["style"]["band"], "guidance": "style only",
                        "style": d["style"]})
    except Exception as e:
        return f"[detector unreachable: {str(e)[:200]}]"


if __name__ == "__main__":
    mcp.run()
