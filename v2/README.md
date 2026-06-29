# v2 — the NOMAD pipeline engine

The core of NOMAD: an **explicit state-machine engine** that drives one unit of work through

```
Capture → Clarify → Route → Process → Human Gate (pause) → Execute → Log & Learn
```

NocoDB-on-Postgres is the source of truth, so the engine is **stateless between pause and
resume** — it reconstructs a run from the `comms` row keyed by `run_id`. This is what makes the
human gate safe: the process can restart and a paused run still resumes correctly.

## How a run flows

1. **Capture** — `POST /capture {goal}` (or the cockpit chat) starts a run.
2. **Clarify / Route** — the Manager clarifies intent and routes to a lane specialist; past
   episodes (`memory.py`) are recalled into context so routing isn't stateless.
3. **Process** — the lane specialist drafts an action proposal via the runtime. The research lane
   can ground its draft in live web data first (`scraper.py` → the `nomad-scraper` service);
   that fetch is read-only and happens **before** the gate.
4. **Human Gate** — the run pauses. The operator flips a `comms` row status to approved/rejected
   (cockpit, NocoDB, or n8n email link). A Postgres `LISTEN/NOTIFY` trigger notifies the engine,
   which resumes in ~0.2s. A poller is kept as a fail-open backup.
5. **Execute** — only the approved action runs (e.g. send the message via an MCP tool).
6. **Log & Learn** — a provenance record (what/why/outcome/lane) is written back.

**Project orchestration:** `engine.plan_project()` decomposes a goal into goals → milestones →
tasks (each task assigned a lane); each task runs through the normal gate, and on execute the
task is marked done and the milestone progress rolls up. With `NOMAD_AUTO_ADVANCE=1` planning
auto-queues the first task and each executed task queues the next (queue-only — every action
still gates).

## Files

| File | Role |
|---|---|
| `server.py` | FastAPI/uvicorn HTTP service + the human-gate entry. Cockpit `/`, API, SSE `/events`. |
| `engine.py` | The pipeline state machine: `start_run`, `resume_run`, `retry_run`, project planning. |
| `specialists.py` | One generic, config-driven `Specialist` per lane. Add a lane = a skill file + an MCP tool + one `LANES` entry. |
| `skills/` | Per-lane skill briefs (system prompts): `comms`, `research`, `dev`, `support`, `ads`. |
| `runtime.py` | Pluggable specialist runtime — **Claude Agent SDK** native (default) with automatic **LiteLLM** fallback. |
| `llm.py` | LiteLLM gateway helper (role aliases only). |
| `nocodb.py` | Minimal stdlib NocoDB client (the source of truth). |
| `memory.py` / `memory_chat.py` | Episodic recall — closes the learn-loop. |
| `scraper.py` | Thin client for the `nomad-scraper` service (research-lane grounding). |
| `mcp_*.py` | MCP tool servers per lane (`comms`, `content`, `dev`, `research`, `image`). |
| `migrate_notion.py` | One-time Notion → NocoDB migration. |
| `setup_nocodb.py` / `setup_gate_trigger.sh` | One-time setup: NocoDB schema + the LISTEN/NOTIFY gate trigger. |
| `test_*.py` | Acceptance tests (plain Python scripts — see below). |

## HTTP API (`server.py`)

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness. |
| `POST /capture {goal}` | Start a run; pauses at the gate. |
| `POST /resume {run_id, decision}` | Resume a paused run (`approved`/`rejected`). |
| `POST /nocodb-hook` | **The gate:** a comms status flip resumes the run. |
| `POST /retry` | Re-queue a failed run at the gate. |
| `GET /runs` · `GET /run?id=` | Recent runs / run drill-down for the cockpit. |
| `GET /events` | SSE push for live cockpit refresh. |
| `POST /generate-image` | Gated image generation (ComfyUI → OpenAI → Firefly fallback chain). |
| `POST /plan-project` · `/run-next-task` · `/task-action` | Project orchestration. |

## Run it

```bash
# In the stack (recommended):
docker compose up -d nomad-v2-engine        # cockpit + API at 127.0.0.1:8099
docker compose build nomad-v2-engine        # rebuild after changes
docker compose logs -f nomad-v2-engine

# Directly (host/dev, from v2/):
uvicorn server:app --host 0.0.0.0 --port 8099

# One-time setup:
bash setup_gate_trigger.sh                   # install the LISTEN/NOTIFY gate trigger
python3 setup_nocodb.py                      # ensure the NocoDB schema/tables exist
```

## Tests

Plain Python scripts (no pytest harness). Run on the host with the NocoDB stack up and
`NC_*` / `LITELLM_*` set in `.env`:

```bash
python3 test_engine.py          # gate pauses; only the approved path executes
python3 test_engine_states.py   # engine state-machine transitions
python3 test_2a.py              # multi-specialist routing + research-lane gate
python3 test_3c.py              # Phase-3 checks
```

## Key environment variables

| Var | Default | Meaning |
|---|---|---|
| `NOMAD_V2_ENGINE_HOST` / `_PORT` | `127.0.0.1` / `8099` | Bind address (localhost-only by default). |
| `NOMAD_V2_GATE_POLL` | `3` | Gate poll interval (s) — the fail-open backup to push. |
| `NOMAD_RUNTIME` | `sdk` if available | Force the runtime path: `sdk` or `litellm`. |
| `NOMAD_AUTO_ADVANCE` | `1` | Auto-queue the next task in a project (still gates). |
| `NC_BASE_URL` | `http://localhost:8095` | NocoDB base. |
| `LITELLM_BASE_URL` / `LITELLM_MASTER_KEY` | — | Model gateway. |
| `NOMAD_VOICE_URL` / `NOMAD_SCRAPER_URL` / `NOMAD_DISPATCH_URL` | service hosts | Companion services. |

See [`../AGENTS.md`](../AGENTS.md) for repo-wide build/test/guardrail conventions and
[`../CLAUDE.md`](../CLAUDE.md) for the architecture overview.
