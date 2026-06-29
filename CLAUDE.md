# Project NOMAD — Context for AI Coding Agents
**Networked Operations & Management Assistant for Decisions**

> Auto-loaded by Claude Code (and other coding agents) when this repo is opened.
> Companion files: **Nomad.md** (discovery marker) · **AGENTS.md** (build/test/guardrail rules)
> · **README.md** (setup) · **NOMAD_Orchestrator_Architecture.md** (full design).

## What this is
NOMAD is a personal AI orchestrator plus an autonomous multi-agent team that runs projects
end-to-end. A manager agent delegates to specialists (comms, research, dev, support, ads) who
work across email, calendar, docs, code, and image generation. Agents act autonomously on
anything reversible but **pause for human approval on irreversible/external actions** (send
email, merge to main, publish, share externally, spend, delete).

This repository is NOMAD itself. NOMAD v2 (below) is the current architecture; v1 services
remain in the tree as the rollback target.

## Architecture — the v2 pipeline
v2 makes the orchestration an **explicit state machine**:

```
Capture → Clarify → Route → Process → Human Gate → Execute → Log & Learn
```

- **Engine** (`v2/server.py`, FastAPI/uvicorn) drives the pipeline; cockpit + API on `:8099`.
- **Human gate** uses Postgres `LISTEN/NOTIFY`: a trigger on the comms table notifies the
  engine, which resumes a paused run in ~0.2 s (a poller is kept as a fail-open backup).
- **Routing** is config-driven (`v2/specialists.py`) across lanes: comms, research, dev,
  support, ads. Each lane has a skill brief in `v2/skills/`.
- **Data layer**: NocoDB-on-Postgres (mission control — projects, goals, milestones, tasks,
  activity, approvals, knowledge). Qdrant holds conversational/agent memory.
- **Models** go through a **LiteLLM** gateway using role aliases (`deep`, `balanced`, `gpt`,
  `longdoc`, `fast`, `private`, `code`) — never raw model names — with local Ollama models as a
  universal fallback.
- **Project orchestration**: `engine.plan_project()` decomposes a goal into goals → milestones →
  tasks (each task assigned a lane); each task runs through the normal gate; on execute the task
  is marked done and the milestone progress rolls up.

## Repository layout
```
v2/                 # the v2 pipeline engine + specialists, skills, tests, MCP tools
nomad-console/      # operator dashboard (telemetry, projects, agents, chat, voice)
crew/               # CrewAI agent team + dev crew (v1; retained)
dispatcher/         # NOMAD → builder handoff: headless Claude Code in a repo (+ termd PTY service)
mcp-local/          # local MCP servers (local LLM bridge, scraper, image gen)
nomad-plugin/       # NOMAD packaged as a Claude Code plugin
nomad-voice/        # on-device STT (faster-whisper) + TTS (Piper)
nomad-scraper/      # LLM-driven web scrape/search (ScrapeGraphAI)
claude-bridge/      # OpenAI-compatible shim that shells to the `claude` CLI
litellm/            # model-gateway routing config
n8n/                # automation + approval-gate workflows (templates)
notion/             # optional Notion mission-control schema + setup
docker-compose.yml  # the full stack
```

## Stack
Python 3.11 (FastAPI + uvicorn) · Docker Compose · Postgres + NocoDB · Qdrant ·
LiteLLM (model gateway) · Ollama (local models) · n8n (automation/gates) · Open WebUI (chat).

## Build, run & test
See **AGENTS.md** for the full command list. Quick start:
```bash
cp .env.example .env          # then fill in keys (see comments in the file)
docker compose up -d          # bring up the stack
docker compose up -d nomad-v2-engine   # engine only → cockpit/API at 127.0.0.1:8099
```
v2 tests are plain Python scripts (no pytest harness), run on the host with the stack up:
```bash
python3 v2/test_engine.py          # gate pauses; only the approved path executes
python3 v2/test_engine_states.py   # engine state-machine transitions
python3 v2/test_2a.py              # multi-specialist routing + research-lane gate
```

## Guardrails (NOMAD convention)
- **Human gate is mandatory** for irreversible/external actions — never bypass it.
- Coding agents make **uncommitted** edits only; humans own `git commit`/`push`.
- Secrets live only in `.env` (never commit). Keep services on `localhost`/Tailscale.
- Agents reference LiteLLM **role aliases**, never raw model names — swap models in
  `litellm/config.yaml` only.

## Configuration notes
- All real configuration is via environment variables — see `.env.example` for the full list
  (model providers, Notion, image generation, voice, the approval webhook). Nothing in the
  tree contains live credentials.
- The n8n workflow JSONs under `n8n/` are **templates**: `YOUR_*` placeholders mark the
  credential and database IDs you attach in your own n8n instance.
