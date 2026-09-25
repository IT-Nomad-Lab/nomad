"""Detection gate for the writing lanes — a thin client to nomad-detect (:8214).

Any lane that writes prose for a human (comms, ads) drafts first and gets checked second. If the
draft reads as machine-written, the specialist gets its own findings back and rewrites ONCE, then
the better of the two drafts goes to the human gate with the detector report attached to the
proposal. The operator approving a message can see what the detector said about it.

Fail-open by design: if the detector is down, the draft ships to the gate unchecked rather than
stalling the pipeline. A missing check is visible in the report (`"checked": false`).
"""
import os

import requests

_CANDIDATES = [c for c in (
    os.environ.get("NOMAD_DETECT_URL"),
    "http://host.docker.internal:8214",     # engine runs in a container, detector is host-native
    "http://127.0.0.1:8214",
    "http://172.21.128.1:8214",
) if c]
TIMEOUT = float(os.environ.get("NOMAD_DETECT_TIMEOUT", "90"))
_base = None


def _url():
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


def detect(text: str) -> dict | None:
    base = _url()
    if not base:
        return None
    try:
        r = requests.post(f"{base}/detect", json={"text": text}, timeout=TIMEOUT)
        d = r.json()
        return d if d.get("ok") else None
    except Exception:
        return None


def _summary(d: dict) -> dict:
    """The small version that rides along in the proposal payload. The full findings list would
    bloat every row in the comms table for no operator benefit."""
    b, s = d.get("binoculars", {}), d.get("style", {})
    return {"verdict": d.get("verdict"), "statistical": b.get("score") if b.get("ok") else None,
            "statistical_band": b.get("band"), "style_score": s.get("score"),
            "findings": s.get("summary", {}).get("total"),
            "top": [f"L{f['line']} {f['rule']}" for f in (d.get("rewrite_hints") or [])[:5]]}


def hints(d: dict) -> str:
    """The findings, formatted as instructions the drafting model can act on."""
    lines = []
    for f in (d.get("rewrite_hints") or [])[:10]:
        lines.append(f"- \"{f['excerpt'][:90]}\" → {f['fix']}")
    return "\n".join(lines)


def enforce(text: str, rewrite=None) -> tuple[str, dict]:
    """Check, optionally rewrite once, return (final_text, report)."""
    first = detect(text)
    if not first:
        return text, {"checked": False, "reason": "detector unreachable"}
    if first.get("passes") or not rewrite:
        return text, {"checked": True, "rewritten": False, **_summary(first)}

    try:
        revised = (rewrite(text, hints(first)) or "").strip()
    except Exception:
        revised = ""
    if not revised:
        return text, {"checked": True, "rewritten": False, **_summary(first)}

    second = detect(revised)
    if not second:
        return revised, {"checked": True, "rewritten": True, "recheck": False, **_summary(first)}
    # Keep the draft that actually reads better, not just the newer one.
    better_second = (second.get("passes") and not first.get("passes")) or (
        (second.get("style", {}).get("score", 0)) > (first.get("style", {}).get("score", 0)))
    final_text, final = (revised, second) if better_second else (text, first)
    return final_text, {"checked": True, "rewritten": better_second, "recheck": True,
                        "before": _summary(first), **_summary(final)}
