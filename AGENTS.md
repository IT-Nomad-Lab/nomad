# NOMAD — Agent Instructions
> Auto-loaded by coding agents (NOMAD dev crew, Claude Code, Codex) in this repo.
> Companions: **Nomad.md** (discovery marker) · **CLAUDE.md** (project context & resume point).

## What this is
NOMAD — a personal AI orchestrator plus an autonomous multi-agent team that runs projects
end-to-end. This is NOMAD's own repository; the v2 pipeline engine (`v2/`) drives the
Capture→Clarify→Route→Process→Human-Gate→Execute→Log&Learn flow with a human approval gate on
irreversible actions.

## Stack
- Python 3.11 (FastAPI + uvicorn) — the v2 engine (`v2/server.py`), console, crew, voice, scraper services.
- Docker Compose — the full stack (LiteLLM gateway, Open WebUI, n8n, NocoDB, Postgres, Qdrant + the NOMAD services).
- Postgres + NocoDB — v2 data layer (db `nomad_v2`); push gate via Postgres LISTEN/NOTIFY.
- Qdrant — conversational/agent memory; LiteLLM — model gateway (role aliases, never raw model names).

## Build & run
```bash
# Full stack
docker compose up -d                      # bring up the whole stack (see README "What runs where")
docker compose up -d nomad-v2-engine      # v2 engine only (cockpit/API at 127.0.0.1:8099)
docker compose build nomad-v2-engine      # rebuild the engine image after v2/ changes
docker compose logs -f nomad-v2-engine    # tail engine logs

# Engine directly (host/dev, from v2/)
uvicorn server:app --host 0.0.0.0 --port 8099   # same CMD as v2/Dockerfile

# One-time v2 setup
bash v2/setup_gate_trigger.sh             # install the Postgres LISTEN/NOTIFY gate trigger
python3 v2/setup_nocodb.py                # ensure the NocoDB schema/tables exist
```

## Test & verify
v2 tests are plain Python scripts (no pytest harness); run them on the host with the NocoDB
stack up and `NC_*` / `LITELLM_*` set in `.env`:
```bash
python3 v2/test_engine.py          # gate pauses; only the approved path executes (live NocoDB + LiteLLM)
python3 v2/test_engine_states.py   # engine state-machine transitions
python3 v2/test_2a.py              # multi-specialist routing + research-lane gate
python3 v2/test_3c.py              # Phase 3 checks
python3 v2/test_income_lab.py      # Income Lab data layer end-to-end (creates + cleans up its own rows)

# Static checks safe for the dispatcher /verify (no mutation, no network):
python3 -m py_compile v2/server.py v2/engine.py   # compile-check changed modules
git diff                                          # confirm edits are additive/scoped
```

## Guardrails (NOMAD convention)
- Make **uncommitted** edits only — never `git commit`/`push` (human-gated).
- Keep changes scoped; don't add dependencies without justification.
- Secrets only in `.env` (never commit); agents reference LiteLLM **role aliases**
  (deep/balanced/gpt/fast/private/code), never raw model names.
- Irreversible/external actions (send email, merge to main, publish, share, spend, delete)
  must pass the human approval gate — do not bypass it.
- v2 builds **alongside** v1; v1 (this repo's CLAUDE.md scope) is the rollback target — don't break it.
