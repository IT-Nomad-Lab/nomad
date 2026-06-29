# crew — the CrewAI agent teams

The original (v1) agent team, retained alongside the [`v2/`](../v2/) pipeline engine. Two crews
live here:

1. **The mission crew** — a manager + specialists (planner, researcher, builder, writer, comms,
   reviewer) that executes a milestone end-to-end and writes results to mission control.
2. **The engineering crew** — an autonomous team that develops and tests **NOMAD itself**: Lead
   Architect → Engineers → QA → Reviewer.

Every agent reasons through a **LiteLLM role alias** (`deep`, `balanced`, `fast`, `code`,
`longdoc`, `private`) — never a raw model name. Swap models in
[`../litellm/config.yaml`](../litellm/config.yaml) only.

> The engineering crew's *hands* are the [`dispatcher/`](../dispatcher/) (headless Claude Code in
> the target repo). It makes **uncommitted** edits and proves them with real checks, but **never
> commits or pushes** — a human reviews the working tree and owns the commit.

## Files

| File | Role |
|---|---|
| `crew.py` | Builds & runs the mission crew. |
| `agents.yaml` | Mission-crew roles: `orchestrator`, `planner`, `researcher`, `builder`, `writer`, `comms`, `reviewer`. |
| `tasks.yaml` | One milestone pass: `plan_milestone` → `execute_milestone` → `review_milestone`. |
| `dev_crew.py` | The engineering crew: design → build (via dispatcher) → QA → review; plus `run_backlog`. |
| `dev_team.yaml` | Engineering-crew roles: lead architect, backend/frontend/integration engineers, QA, reviewer. |
| `server.py` | HTTP triggers (FastAPI). |
| `tools/` | Agent tools: `notion_tools.py` (shared memory + approval gate), `dev_tools.py` (dispatch/verify), `firefly_tool.py` (image gen). |

## HTTP API (`server.py`)

| Endpoint | Body | Purpose |
|---|---|---|
| `GET /health` | — | Liveness. |
| `POST /run-milestone` | `{goal, criteria, milestone}` | Run one milestone end-to-end. |
| `POST /run-dev` | `{goal, project}` | Engineering crew builds one feature (design→build→test→review). |
| `POST /run-backlog` | `{limit}` | Work the mission-control backlog hands-off (background thread). |

## Run it

```bash
docker compose up -d crew     # → http://localhost:8001

curl -X POST http://localhost:8001/run-milestone \
  -H "Content-Type: application/json" \
  -d '{"goal":"Launch the landing page","criteria":"Live, mobile-friendly","milestone":"Draft copy and hero image"}'
```

## Key environment variables

| Var | Meaning |
|---|---|
| `LITELLM_BASE_URL` / `LITELLM_MASTER_KEY` | Model gateway every agent points at. |
| `NOMAD_DISPATCH_URL` | The dispatcher the engineering crew uses to edit repos. |
| `NOTION_TOKEN` / `NOTION_DB_*` | Mission-control writes (shared-memory tools). |

## Notes

- `crew/tools/firefly_tool.py` targets the Adobe Firefly API — verify endpoint paths/payloads
  against the current Firefly version before relying on image generation.
- High-stakes actions (send email, merge, share, spend) go through `request_approval` → the n8n
  approval gate; nothing irreversible runs without a human tap.
