"""NOMAD — web scraping service (optimized: fast static path + LLM/browser fallback).

Design: most pages are static HTML, so we DON'T launch a browser or an LLM by default.

  1. FAST PATH   — httpx fetch (real UA, retries, per-domain rate-limit) → trafilatura extracts the
                   clean article text (~1s, free). This is what grounding a brief actually needs.
  2. DISTILL     — only when a `schema` or `distill` is requested, run the LOCAL model (LiteLLM
                   `fast` alias, free) over the CLEAN text (few tokens) to answer/extract.
  3. HEAVY PATH  — if the static fetch yields nothing (JS-heavy/blocked), fall back to
                   ScrapeGraphAI (headless Chromium + LLM) — the old behavior, now the exception.

Plus: a TTL result cache, async handlers, parallel deepening in /search, and a self-hosted
SearXNG search backend (no keys) with a ddgs fallback.

Endpoints:
  GET  /health
  POST /scrape  {url, prompt?, schema?, distill?, no_cache?}   → {ok, url, mode, title, result}
  POST /search  {query, prompt?, max?, deepen?, schema?}       → {ok, query, result, sources}
"""
import asyncio
import json
import os
import time
from urllib.parse import urlparse

import httpx
import trafilatura
from cachetools import TTLCache
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

# ── config ───────────────────────────────────────────────────────────────────────────
LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000").rstrip("/")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-noop")
SCRAPER_MODEL = os.environ.get("SCRAPER_MODEL", "fast")        # default LOCAL (free); gpt for hard cases
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
EMBED_MODEL = os.environ.get("NOMAD_EMBED_MODEL", "nomic-embed-text")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
MAX_RESULTS = int(os.environ.get("SCRAPER_MAX_RESULTS", "5"))
DEEPEN_DEFAULT = int(os.environ.get("SCRAPER_DEEPEN", "3"))     # top-N results to fetch full text for
CACHE_TTL = int(os.environ.get("SCRAPER_CACHE_TTL", "3600"))
FETCH_TIMEOUT = float(os.environ.get("SCRAPER_FETCH_TIMEOUT", "20"))
DOMAIN_INTERVAL = float(os.environ.get("SCRAPER_DOMAIN_INTERVAL", "1.0"))  # min secs between hits/host
MAX_TEXT = int(os.environ.get("SCRAPER_MAX_TEXT", "20000"))
UA = os.environ.get("SCRAPER_UA",
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36 NOMAD-scraper")

_cache: TTLCache = TTLCache(maxsize=1024, ttl=CACHE_TTL)
_last_hit: dict[str, float] = {}
_host_locks: dict[str, asyncio.Lock] = {}

# Compat shim for the ScrapeGraphAI fallback (langchain 1.x moved ChatOllama).
try:
    import langchain_community.chat_models as _ccm
    if not hasattr(_ccm, "ChatOllama"):
        from langchain_ollama import ChatOllama as _ChatOllama
        _ccm.ChatOllama = _ChatOllama
except Exception:
    pass

app = FastAPI(title="nomad-scraper")


class ScrapeReq(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    url: str
    prompt: str = "Extract the main content, key facts, and important details."
    schema_: dict | None = Field(default=None, alias="schema")  # JSON schema → structured LLM extract
    distill: bool = False                                        # run the local LLM over the clean text
    no_cache: bool = False


class SearchReq(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    query: str
    prompt: str = "Summarize the key facts relevant to the query, with sources."
    max: int | None = None
    deepen: int | None = None
    schema_: dict | None = Field(default=None, alias="schema")


# ── helpers ──────────────────────────────────────────────────────────────────────────
async def _rate_limit(host: str):
    """At most one hit per DOMAIN_INTERVAL per host — polite, avoids trivial bans."""
    lock = _host_locks.setdefault(host, asyncio.Lock())
    async with lock:
        wait = DOMAIN_INTERVAL - (time.monotonic() - _last_hit.get(host, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last_hit[host] = time.monotonic()


async def _fetch(url: str) -> str | None:
    """Fetch raw HTML with a real UA + retries + per-domain rate-limit. None on failure."""
    host = urlparse(url).netloc
    await _rate_limit(host)
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT,
                                         headers={"User-Agent": UA}) as c:
                r = await c.get(url)
            if r.status_code == 200 and r.text:
                return r.text
            if r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return None
        except Exception:
            await asyncio.sleep(1.0 * (attempt + 1))
    return None


def _extract(html: str, url: str) -> tuple[str | None, str | None]:
    """trafilatura → (clean_text, title). Runs in a threadpool (CPU-bound)."""
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True,
                               favor_precision=True)
    title = None
    try:
        md = trafilatura.extract_metadata(html)
        title = getattr(md, "title", None) if md else None
    except Exception:
        pass
    return (text or None), title


async def _llm(prompt: str, text: str, schema: dict | None) -> str | dict:
    """Distill/extract over CLEAN text via the LiteLLM gateway (default local `fast` model)."""
    sys = ("You extract information from web page text. Use ONLY the provided text; be accurate and "
           "concise. If asked for JSON, output only valid JSON.")
    user = f"TASK: {prompt}\n\nPAGE TEXT:\n{text[:MAX_TEXT]}"
    if schema:
        user += f"\n\nReturn ONLY JSON matching this schema:\n{json.dumps(schema)}"
    body = {"model": SCRAPER_MODEL, "messages": [{"role": "system", "content": sys},
                                                 {"role": "user", "content": user}],
            "max_tokens": 900, "temperature": 0}
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{LITELLM_BASE}/v1/chat/completions",
                         headers={"Authorization": f"Bearer {LITELLM_KEY}"}, json=body)
    r.raise_for_status()
    out = r.json()["choices"][0]["message"]["content"].strip()
    if schema:
        try:
            return json.loads(out[out.find("{"):out.rfind("}") + 1])
        except Exception:
            return out
    return out


def _heavy_scrape(url: str, prompt: str, schema: dict | None) -> dict | str:
    """Fallback: ScrapeGraphAI (headless Chromium + LLM). Runs in a threadpool."""
    from scrapegraphai.graphs import SmartScraperGraph
    cfg = {"llm": {"api_key": LITELLM_KEY, "model": f"openai/{SCRAPER_MODEL}",
                   "base_url": f"{LITELLM_BASE}/v1"},
           "embeddings": {"model": f"ollama/{EMBED_MODEL}", "base_url": OLLAMA_URL},
           "headless": True, "verbose": False}
    g = SmartScraperGraph(prompt=prompt, source=url, config=cfg, schema=schema)
    return g.run()


async def _scrape_one(url: str, prompt: str, schema: dict | None, distill: bool) -> dict:
    """Fast static path with heavy fallback → {mode, title, result}."""
    html = await _fetch(url)
    if html:
        text, title = await asyncio.to_thread(_extract, html, url)
        if text and len(text) > 200:
            if schema or distill:
                try:
                    return {"mode": "static+llm", "title": title,
                            "result": await _llm(prompt, text, schema)}
                except Exception:
                    pass   # distill failed → fall through to returning clean text
            return {"mode": "static", "title": title, "result": text[:MAX_TEXT]}
    # static failed or too thin → heavy browser+LLM fallback
    try:
        res = await asyncio.to_thread(_heavy_scrape, url, prompt, schema)
        return {"mode": "browser+llm", "title": None, "result": res}
    except Exception as e:
        return {"mode": "failed", "title": None, "result": None,
                "error": f"{type(e).__name__}: {str(e)[:200]}"}


async def _search_backend(query: str, n: int) -> list[dict]:
    """SearXNG JSON (no keys) → snippets; ddgs fallback. [{title,url,snippet}]."""
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as c:
            r = await c.get(f"{SEARXNG_URL}/search",
                            params={"q": query, "format": "json", "safesearch": 0})
        if r.status_code == 200:
            rows = r.json().get("results", [])[:n]
            out = [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content")}
                   for x in rows if x.get("url")]
            if out:
                return out
    except Exception:
        pass
    # fallback: ddgs (DuckDuckGo)
    try:
        from ddgs import DDGS
        hits = list(DDGS().text(query, max_results=n)) or []
        return [{"title": h.get("title"), "url": h.get("href"), "snippet": h.get("body")}
                for h in hits if h.get("href")]
    except Exception:
        return []


# ── endpoints ────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "service": "nomad-scraper", "model": SCRAPER_MODEL,
            "search": SEARXNG_URL, "cache": len(_cache)}


@app.post("/scrape")
async def scrape(req: ScrapeReq):
    key = ("scrape", req.url, req.prompt, bool(req.schema_), req.distill, SCRAPER_MODEL)
    if not req.no_cache and key in _cache:
        return {**_cache[key], "cached": True}
    r = await _scrape_one(req.url, req.prompt, req.schema_, req.distill)
    resp = {"ok": r.get("result") is not None, "url": req.url, **r}
    if resp["ok"] and not req.no_cache:
        _cache[key] = resp
    return resp


@app.post("/search")
async def search(req: SearchReq):
    n = req.max or MAX_RESULTS
    deep = DEEPEN_DEFAULT if req.deepen is None else req.deepen
    key = ("search", req.query, req.prompt, n, deep, bool(req.schema_), SCRAPER_MODEL)
    if key in _cache:
        return {**_cache[key], "cached": True}
    snippets = await _search_backend(req.query, n)
    if not snippets:
        return {"ok": False, "error": "no search results", "query": req.query}
    sources = [s["url"] for s in snippets]
    # deepen the top `deep` results IN PARALLEL (fast static path each)
    pages = []
    if deep > 0:
        got = await asyncio.gather(*[_scrape_one(u, req.prompt, req.schema_, bool(req.schema_))
                                     for u in sources[:deep]], return_exceptions=True)
        pages = [{"url": u, **g} for u, g in zip(sources[:deep], got)
                 if isinstance(g, dict) and g.get("result")]
    resp = {"ok": True, "query": req.query,
            "result": {"search_results": snippets, "pages": pages}, "sources": sources}
    _cache[key] = resp
    return resp
