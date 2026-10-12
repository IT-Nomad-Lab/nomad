# Project NOMAD — Context for AI Coding Agents
**Networked Operations & Management Assistant for Decisions**

> Auto-loaded by Claude Code. Companions: **Nomad.md** (discovery marker) · **AGENTS.md**
> (build/test/guardrail rules) · **README.md** (setup).

## What this is
NOMAD is a personal AI orchestrator plus an autonomous multi-agent team that runs projects
end-to-end. A manager agent delegates to specialists (comms, research, dev, support, ads).
Agents act autonomously on anything reversible but **pause for human approval on
irreversible/external actions** (send email, merge to main, publish, share, spend, delete).

This repository is NOMAD itself. NOMAD v2 is the current architecture; v1 services remain in
the tree as the rollback target.

## Map
v2 pipeline: Capture → Clarify → Route → Process → Human Gate → Execute → Log & Learn.
Engine `v2/server.py` (FastAPI, cockpit + API on `:8099`); lanes in `v2/specialists.py`, skill
briefs in `v2/skills/`. Other dirs: `nomad-console/` `crew/` (v1) `dispatcher/` `mcp-local/`
`nomad-plugin/` `nomad-voice/` `nomad-scraper/` `claude-bridge/` `litellm/` `n8n/` `notion/`.
Full architecture, directory layout and configuration notes (`.env.example`, n8n `YOUR_*`
templates): [agent-docs/architecture.md](agent-docs/architecture.md). Design: NOMAD_Orchestrator_Architecture.md.

## Stack
Python 3.11 (FastAPI + uvicorn) · Docker Compose · Postgres + NocoDB · Qdrant ·
LiteLLM · Ollama · n8n · Open WebUI.

## Build, run & test
See **AGENTS.md** for the full command list. Quick start:
```bash
cp .env.example .env          # then fill in keys (see comments in the file)
docker compose up -d          # bring up the stack
docker compose up -d nomad-v2-engine   # engine only → cockpit/API at 127.0.0.1:8099
```
v2 tests are plain Python scripts (no pytest), run on the host with the stack up:
```bash
python3 v2/test_engine.py          # gate pauses; only the approved path executes
python3 v2/test_engine_states.py   # engine state-machine transitions
python3 v2/test_2a.py              # multi-specialist routing + research-lane gate
```

## Guardrails (NOMAD convention)
- **Human gate is mandatory** for irreversible/external actions — never bypass it.
- Coding agents make **uncommitted** edits only; humans own `git commit`/`push`.
- Secrets live only in `.env` (never commit). Keep services on `localhost`/Tailscale.
- Agents reference LiteLLM **role aliases** (`deep`, `balanced`, `gpt`, `longdoc`, `fast`,
  `private`, `code`), never raw model names — swap models in `litellm/config.yaml` only.
- Nothing in the tree contains live credentials; all config is via environment variables.

## Where to find more
- Architecture details (human gate, routing, data layer, models, project orchestration),
  repository layout, configuration notes: [agent-docs/architecture.md](agent-docs/architecture.md)
