"""NOMAD — web scraping service (ScrapeGraphAI behind a small HTTP API).

LLM-driven scraping: give it a URL (or a search query) + a natural-language prompt, and it returns
structured data. It runs ScrapeGraphAI pointed at NOMAD's existing **LiteLLM gateway** (so no new
keys, and the model is swappable by alias), with embeddings on the local Ollama. Heavy deps
(LangChain, Playwright/Chromium) live here so the slim engine stays slim — the engine's research
lane calls this over HTTP during Process (read-only/reversible; the brief is gated separately).

Endpoints:
  GET  /health                          → {ok, model}
  POST /scrape  {url, prompt}           → {ok, url, result}            (one page, SmartScraperGraph)
  POST /search  {query, prompt, max?}   → {ok, query, result, sources} (search + scrape, SearchGraph)
"""
import os

from fastapi import FastAPI
from pydantic import BaseModel

# Compat shim: scrapegraphai 1.x imports `ChatOllama` from langchain_community.chat_models, but the
# langchain 1.x stack moved it to langchain_ollama. Re-inject it before scrapegraphai is imported.
try:
    import langchain_community.chat_models as _ccm
    if not hasattr(_ccm, "ChatOllama"):
        from langchain_ollama import ChatOllama as _ChatOllama
        _ccm.ChatOllama = _ChatOllama
except Exception:
    pass

# LLM backend = the LiteLLM gateway (OpenAI-compatible). Model picked by NOMAD alias.
LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000").rstrip("/")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-noop")
SCRAPER_MODEL = os.environ.get("SCRAPER_MODEL", "gpt")          # alias: gpt (reliable) / fast (local)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
EMBED_MODEL = os.environ.get("NOMAD_EMBED_MODEL", "nomic-embed-text")
MAX_RESULTS = int(os.environ.get("SCRAPER_MAX_RESULTS", "3"))


def _graph_config(extra: dict | None = None) -> dict:
    """ScrapeGraphAI config: LLM via LiteLLM (OpenAI-compatible), embeddings via local Ollama."""
    cfg = {
        "llm": {
            "api_key": LITELLM_KEY,
            "model": f"openai/{SCRAPER_MODEL}",     # 'openai/<alias>' → LiteLLM resolves the alias
            "base_url": f"{LITELLM_BASE}/v1",
        },
        "embeddings": {
            "model": f"ollama/{EMBED_MODEL}",
            "base_url": OLLAMA_URL,
        },
        "headless": True,
        "verbose": False,
    }
    if extra:
        cfg.update(extra)
    return cfg


app = FastAPI(title="nomad-scraper")


class ScrapeReq(BaseModel):
    url: str
    prompt: str = "Extract the main content, key facts, and any important details from this page."


class SearchReq(BaseModel):
    query: str
    prompt: str = "Summarize the key facts and findings relevant to the query, with sources."
    max: int | None = None


@app.get("/health")
def health():
    return {"ok": True, "service": "nomad-scraper", "model": SCRAPER_MODEL,
            "llm_base": LITELLM_BASE}


@app.post("/scrape")
def scrape(req: ScrapeReq):
    """Scrape a single URL with an extraction prompt → structured result."""
    from scrapegraphai.graphs import SmartScraperGraph
    try:
        g = SmartScraperGraph(prompt=req.prompt, source=req.url, config=_graph_config())
        result = g.run()
        return {"ok": True, "url": req.url, "result": result}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}"}


@app.post("/search")
def search(req: SearchReq):
    """Search the web and return grounded evidence. Built on robust pieces rather than
    ScrapeGraphAI's fragile SearchGraph merge: DuckDuckGo (`ddgs`) gives titled snippets (always
    available), then SmartScraperGraph deepens the top hit (best-effort). Returns snippets + the
    scraped top page + sources, so the caller always has something to ground on."""
    n = req.max or MAX_RESULTS
    try:
        from ddgs import DDGS
        hits = list(DDGS().text(req.query, max_results=n)) or []
    except Exception as e:
        return {"ok": False, "error": f"search failed: {type(e).__name__}: {str(e)[:200]}"}
    if not hits:
        return {"ok": False, "error": "no search results"}
    snippets = [{"title": h.get("title"), "url": h.get("href"), "snippet": h.get("body")}
                for h in hits]
    sources = [h.get("href") for h in hits if h.get("href")]
    # deepen: scrape the top result for fuller content (best-effort — snippets stand alone if it fails)
    top_page = None
    try:
        from scrapegraphai.graphs import SmartScraperGraph
        g = SmartScraperGraph(prompt=req.prompt, source=sources[0], config=_graph_config())
        top_page = g.run()
    except Exception:
        top_page = None
    return {"ok": True, "query": req.query,
            "result": {"search_results": snippets, "top_page": top_page},
            "sources": sources}
