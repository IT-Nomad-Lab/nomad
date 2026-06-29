# mcp-local — NOMAD's local agents, exposed to Claude Code

Phase 1 of using **Claude Code as the orchestrator** over local + cloud agents. These are MCP
servers Claude Code loads (via the repo's `.mcp.json`) so it can call your local capabilities as
tools — keeping cheap/private/bulk work on your hardware while Claude conducts.

## Tools

**`nomad-local`** (`nomad_local.py`) — delegate to a local model on your GPU (native Ollama):
- `local_llm(prompt, system, model, max_tokens)` — run a prompt locally. `model` is a role alias
  (`fast`=llama3.1:8b, `private`=deepseek-r1:32b, `code`=qwen2.5-coder:32b) or a full Ollama tag.
- `list_local_models()` — what's installed.

**`nomad-scrape`** (`nomad_scrape.py`) — web research from the editor (the nomad-scraper service):
- `scrape(url, prompt)` — scrape one page.
- `web_search(query, prompt)` — search + scrape top results.

Both auto-detect their endpoint (Ollama on the Windows host; the scraper container on loopback or
the host gateway), so they work whether Claude Code runs in WSL or natively.

## Setup
Deps live in `.venv` (gitignored): `python3 -m venv .venv && .venv/bin/pip install mcp requests`.
`.mcp.json` at the repo root points Claude Code at `.venv/bin/python` running each server.
After it's created, **enable the servers when Claude Code prompts** (project MCP servers need
approval), or reload the window.

## Notes
- `nomad-local` is verified end-to-end. `nomad-scrape` needs the editor's process to reach the
  scraper container (`:8210`) — if it reports "scraper error", we bind the scraper to a
  WSL-reachable interface (+ a token) as a quick follow-up.
- Next phases: expose the repo Builder + the gated pipeline as MCP, and move the approval gate into
  a `PreToolUse` hook so irreversible actions pause inside Claude Code.
