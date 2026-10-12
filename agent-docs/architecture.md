# NOMAD architecture, layout and configuration

Moved from CLAUDE.md (task #1100); text unchanged.

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

## Configuration notes
- All real configuration is via environment variables — see `.env.example` for the full list
  (model providers, Notion, image generation, voice, the approval webhook). Nothing in the
  tree contains live credentials.
- The n8n workflow JSONs under `n8n/` are **templates**: `YOUR_*` placeholders mark the
  credential and database IDs you attach in your own n8n instance.
