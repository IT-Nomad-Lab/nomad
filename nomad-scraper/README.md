# nomad-scraper — LLM-driven web scrape & search

LLM-driven web scraping behind a small HTTP API. Give it a URL (or a search query) plus a
natural-language prompt, and it returns **structured data**. It runs
[ScrapeGraphAI](https://github.com/ScrapeGraphAI/Scrapegraph-ai) pointed at NOMAD's existing
**LiteLLM gateway** — so no new API keys, and the model is swappable by alias — with embeddings
on the local Ollama.

The heavy deps (LangChain, Playwright/Chromium) live here so the engine stays slim. The engine's
**research lane** calls this over HTTP during Process to ground a brief in live web data; that
fetch is read-only/reversible and happens **before** the human gate (only the resulting brief is
gated). Callers fail open — if the scraper is down/slow, they fall back to ungrounded reasoning.

| Endpoint | Body | Returns |
|---|---|---|
| `GET /health` | — | `{ok, model}` |
| `POST /scrape` | `{url, prompt}` | `{ok, url, result}` — one page (SmartScraperGraph) |
| `POST /search` | `{query, prompt, max?}` | `{ok, query, result, sources}` — search + scrape |

## Run it

```bash
docker compose up -d nomad-scraper     # host-only :8210
```

## Key environment variables

| Var | Default | Meaning |
|---|---|---|
| `LITELLM_BASE_URL` / `LITELLM_MASTER_KEY` | gateway | Model gateway. |
| `SCRAPER_MODEL` | `gpt` | LiteLLM alias for scraping (`gpt` reliable / `fast` local). |
| `OLLAMA_URL` / `NOMAD_EMBED_MODEL` | Ollama / `nomic-embed-text` | Embeddings. |
| `SCRAPER_MAX_RESULTS` | `3` | Max search results to scrape. |

> Compat notes baked in: a `ChatOllama` shim (langchain moved it) and `ddgs` (duckduckgo
> renamed); `/search` is rebuilt on `ddgs` + `SmartScraperGraph`.
