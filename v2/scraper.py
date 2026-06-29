"""NOMAD v2 — thin client for the nomad-scraper service (ScrapeGraphAI behind HTTP).

The research lane calls this during Process to ground a brief in real web data. Read-only and
reversible (fetching public pages), so it runs BEFORE the human gate — only the resulting brief is
gated. Fail-open: if the scraper is down/slow, callers fall back to ungrounded reasoning.
"""
import json
import os

import requests

SCRAPER_URL = os.environ.get("NOMAD_SCRAPER_URL", "http://nomad-scraper:8210").rstrip("/")
TIMEOUT = int(os.environ.get("NOMAD_SCRAPER_TIMEOUT", "180"))


def _looks_like_url(s: str) -> bool:
    s = (s or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def gather(intent: str, target: str, prompt: str = "") -> dict:
    """Pick the right mode: a URL target → scrape that page; otherwise → web search + scrape.
    Returns {ok, mode, result, sources, error?}. Never raises (fail-open)."""
    p = prompt or (intent or "Extract the key facts relevant to the request.")
    try:
        if _looks_like_url(target):
            r = requests.post(f"{SCRAPER_URL}/scrape",
                              json={"url": target.strip(), "prompt": p}, timeout=TIMEOUT)
            d = r.json()
            return {"ok": bool(d.get("ok")), "mode": "scrape", "result": d.get("result"),
                    "sources": [target.strip()] if d.get("ok") else [], "error": d.get("error")}
        query = (target or intent or "").strip()
        r = requests.post(f"{SCRAPER_URL}/search",
                          json={"query": query, "prompt": p}, timeout=TIMEOUT)
        d = r.json()
        return {"ok": bool(d.get("ok")), "mode": "search", "result": d.get("result"),
                "sources": d.get("sources", []), "error": d.get("error")}
    except requests.RequestException as e:
        return {"ok": False, "mode": "none", "result": None, "sources": [],
                "error": f"scraper unreachable: {str(e)[:160]}"}


def as_evidence(g: dict, limit: int = 3500) -> str:
    """Format a gather() result as an evidence block for a specialist prompt (or '' if nothing)."""
    if not g or not g.get("ok") or not g.get("result"):
        return ""
    res = g["result"]
    body = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, indent=2)
    src = "\n".join(f"- {s}" for s in (g.get("sources") or [])[:8])
    block = f"WEB EVIDENCE ({g.get('mode')}):\n{body[:limit]}"
    if src:
        block += f"\n\nSOURCES:\n{src}"
    return block
