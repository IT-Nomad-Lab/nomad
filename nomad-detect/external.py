"""Optional third-party detector adapters. OFF unless a key is set.

Everything else in nomad-detect runs on your hardware. This file is the one place that sends text
to somebody else's server, so it is opt-in per provider, it fails open, and it never runs unless a
caller asks for it AND the key exists.

Use it when the text has to clear a checker you do not control (a client's editor, a university,
a marketplace). The local score tells you whether the writing is machine-shaped; the external score
tells you what one specific vendor will say about it today.

  GPTZERO_API_KEY        gptzero.me         (per-document, has a free tier)
  ORIGINALITY_API_KEY    originality.ai     (per-credit, paid)
  SAPLING_API_KEY        sapling.ai         (per-call, free dev tier)

Each returns a 0..1 probability that the text is AI-generated (so HIGHER is worse, the opposite of
the Binoculars score). UNVERIFIED: written against each vendor's published API shape, never run
here, because no key exists on this box. Check the first live response before trusting the number.
"""
import os
from typing import Any

import httpx

TIMEOUT = float(os.environ.get("NOMAD_DETECT_EXTERNAL_TIMEOUT", "30"))


def available() -> list[str]:
    return [n for n, k in (("gptzero", "GPTZERO_API_KEY"),
                           ("originality", "ORIGINALITY_API_KEY"),
                           ("sapling", "SAPLING_API_KEY")) if os.environ.get(k)]


def _gptzero(text: str, key: str) -> dict[str, Any]:
    r = httpx.post("https://api.gptzero.me/v2/predict/text",
                   headers={"x-api-key": key, "Content-Type": "application/json"},
                   json={"document": text, "multilingual": False}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()["documents"][0]
    return {"ai_probability": d.get("completely_generated_prob"),
            "verdict": d.get("predicted_class") or d.get("document_classification")}


def _originality(text: str, key: str) -> dict[str, Any]:
    r = httpx.post("https://api.originality.ai/api/v1/scan/ai",
                   headers={"X-OAI-API-KEY": key, "Content-Type": "application/json"},
                   json={"content": text}, timeout=TIMEOUT)
    r.raise_for_status()
    s = r.json().get("score", {})
    return {"ai_probability": s.get("ai"), "verdict": "ai" if (s.get("ai") or 0) > 0.5 else "human"}


def _sapling(text: str, key: str) -> dict[str, Any]:
    r = httpx.post("https://api.sapling.ai/api/v1/aidetect",
                   json={"key": key, "text": text}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    return {"ai_probability": d.get("score"),
            "verdict": "ai" if (d.get("score") or 0) > 0.5 else "human"}


PROVIDERS = {"gptzero": ("GPTZERO_API_KEY", _gptzero),
             "originality": ("ORIGINALITY_API_KEY", _originality),
             "sapling": ("SAPLING_API_KEY", _sapling)}


def check(text: str, providers: list[str] | None = None) -> dict[str, Any]:
    """Run every configured provider. Fails open per provider: a dead vendor never blocks a draft."""
    out: dict[str, Any] = {}
    for name in (providers or available()):
        env, fn = PROVIDERS.get(name, (None, None))
        key = os.environ.get(env) if env else None
        if not fn:
            out[name] = {"error": "unknown provider"}
        elif not key:
            out[name] = {"error": f"no {env} set"}
        else:
            try:
                out[name] = fn(text, key)
            except Exception as e:
                out[name] = {"error": str(e)[:200]}
    return out
