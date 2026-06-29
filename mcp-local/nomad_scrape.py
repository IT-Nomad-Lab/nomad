"""NOMAD scraper bridge (MCP) — LLM-driven web scrape/search from inside Claude Code.

Wraps the local nomad-scraper service (ScrapeGraphAI) so Claude can ground its work in live web
data without leaving the editor. The scraping runs on your hardware via the LiteLLM gateway.

Tools:
  scrape(url, prompt)        → scrape one page with an extraction prompt → structured data
  web_search(query, prompt)  → search the web + scrape top results → snippets + page + sources
"""
import json
import os

import requests
from mcp.server.fastmcp import FastMCP

# The scraper runs in a container; from the editor's process it may be reachable on loopback or via
# the Windows-host gateway depending on the Docker/WSL setup — so probe a few and reuse what works.
_CANDIDATES = [c for c in (
    os.environ.get("NOMAD_SCRAPER_URL"),
    "http://127.0.0.1:8210",
    "http://host.docker.internal:8210",
    "http://172.21.128.1:8210",
) if c]
_base = None


def _scraper():
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


mcp = FastMCP("nomad-scrape")


@mcp.tool()
def scrape(url: str, prompt: str = "Extract the key facts and main content of this page.") -> str:
    """Scrape a single URL with a natural-language extraction prompt (LLM-driven, renders JS pages).
    Returns the structured result as text. Use when you have a specific page to read."""
    try:
        r = requests.post(f"{_scraper()}/scrape", json={"url": url, "prompt": prompt}, timeout=200)
        d = r.json()
        if not d.get("ok"):
            return f"[scrape failed: {d.get('error')}]"
        return json.dumps(d.get("result"), indent=2, ensure_ascii=False)[:8000]
    except Exception as e:
        return f"[scraper error: {str(e)[:240]}]"


@mcp.tool()
def web_search(query: str, prompt: str = "Summarize the key findings relevant to the query.") -> str:
    """Search the web for a query and scrape the top results (DuckDuckGo + ScrapeGraphAI). Returns
    titled snippets, a scraped top page, and the source URLs. Use to research a topic from scratch."""
    try:
        r = requests.post(f"{_scraper()}/search", json={"query": query, "prompt": prompt}, timeout=260)
        d = r.json()
        if not d.get("ok"):
            return f"[search failed: {d.get('error')}]"
        out = d.get("result", {}) or {}
        out["sources"] = d.get("sources", [])
        return json.dumps(out, indent=2, ensure_ascii=False)[:8000]
    except Exception as e:
        return f"[scraper error: {str(e)[:240]}]"


if __name__ == "__main__":
    mcp.run()
