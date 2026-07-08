# nomad-scraper — fast web scrape & search

Grounds NOMAD's research lane in live web data. Optimized so the **common case is fast and free** —
it does *not* launch a browser or an LLM by default.

## How it works (3 tiers)

1. **Fast static path** — `httpx` fetch (real UA, retries, per-domain rate-limit) → **trafilatura**
   extracts the clean article text (~1 s, no browser, no LLM). This is what grounding a brief needs.
2. **Distill** *(opt-in)* — when a `schema` or `distill:true` is requested, the **local `fast`
   model** (via LiteLLM, free) runs over the *clean* text (few tokens) to answer / extract structured data.
3. **Heavy fallback** — if the static fetch yields nothing (JS-heavy/blocked page), fall back to
   **ScrapeGraphAI** (headless Chromium + LLM) — the old behavior, now the exception.

Plus a **TTL result cache**, **async** handlers, **parallel** deepening in `/search`, and a
self-hosted **SearXNG** search backend (no API keys) with a `ddgs` (DuckDuckGo) fallback.

| Endpoint | Body | Returns |
|---|---|---|
| `GET /health` | — | `{ok, model, search, cache}` |
| `POST /scrape` | `{url, prompt?, schema?, distill?, no_cache?}` | `{ok, url, mode, title, result}` |
| `POST /search` | `{query, prompt?, max?, deepen?, schema?}` | `{ok, query, result:{search_results, pages}, sources}` |

`mode` tells you which tier ran: `static` · `static+llm` · `browser+llm`. Pass `schema` (a JSON
schema) for structured extraction; `distill:true` to summarize/answer via the local model.

## Run it

```bash
docker compose up -d searxng nomad-scraper     # scraper host-only :8210, SearXNG :8211 (debug)
```

## Key environment variables

| Var | Default | Meaning |
|---|---|---|
| `SCRAPER_MODEL` | `fast` | LiteLLM alias when an LLM is needed (`fast` local/free · `gpt` for hard cases). |
| `SEARXNG_URL` | `http://searxng:8080` | Self-hosted metasearch; `ddgs` used if it's down. |
| `SCRAPER_MAX_RESULTS` / `SCRAPER_DEEPEN` | `5` / `3` | Search results returned / how many to fetch full text for (parallel). |
| `SCRAPER_CACHE_TTL` | `3600` | Result cache TTL (seconds). |
| `SCRAPER_DOMAIN_INTERVAL` | `1.0` | Min seconds between hits to the same host (politeness). |
| `LITELLM_BASE_URL` / `LITELLM_MASTER_KEY` | gateway | Model gateway (distill + heavy fallback only). |
| `OLLAMA_URL` / `NOMAD_EMBED_MODEL` | Ollama / `nomic-embed-text` | Embeddings (heavy fallback only). |

## Notes

- **SearXNG** needs the JSON format enabled — configured in `searxng/settings.yml`
  (`search.formats: [html, json]`, limiter off for the trusted local caller).
- Compat shim kept for the fallback: `ChatOllama` moved in langchain 1.x; `ddgs` is the renamed
  duckduckgo search.
