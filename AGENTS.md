# NOMAD — Agent Instructions
> Auto-loaded by coding agents (NOMAD dev crew, Claude Code, Codex) in this repo.
> Companions: **Nomad.md** (discovery marker) · **CLAUDE.md** (project context & resume point).

## What this is
NOMAD — a personal AI orchestrator plus an autonomous multi-agent team that runs projects
end-to-end. This is NOMAD's own repo; the v2 pipeline engine (`v2/`) drives the
Capture→Clarify→Route→Process→Human-Gate→Execute→Log&Learn flow with a human approval gate on
irreversible actions.

## Stack
- Python 3.11 (FastAPI + uvicorn) — the v2 engine (`v2/server.py`), console, crew, voice, scraper services.
- Docker Compose — the full stack (LiteLLM gateway, Open WebUI, n8n, NocoDB, Postgres, Qdrant + the NOMAD services).
- Postgres + NocoDB — v2 data layer (db `nomad_v2`); push gate via Postgres LISTEN/NOTIFY.
- Qdrant — conversational/agent memory; LiteLLM — model gateway (role aliases, never raw model names).

## Build & run
```bash
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
v2 has a pytest suite (`v2/test_*.py`). Most of it runs offline; a few scripts need the live
stack (NocoDB + LiteLLM up, `NC_*` / `LITELLM_*` set in `.env`).
```bash
# Offline suite (~950 tests, under a minute): pytest in the engine image, no network.
# test_engine.py, test_2a.py, test_3c.py need the live stack and are excluded. nomad_projects is
# mounted over the image's copy so the tests run the checkout's, not the last build's (#1372).
docker run --rm --network none -u "$(id -u):$(id -g)" -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp \
  --mount type=tmpfs,destination=/app/generated_images,tmpfs-mode=1777 -v "$PWD/v2":/w -w /w \
  -v "$PWD/nomad_projects":/app/lib/nomad_projects:ro \
  --entrypoint sh nomad-nomad-v2-engine -c 'python -m pytest -p no:cacheprovider -q \
  --ignore=test_engine.py --ignore=test_2a.py --ignore=test_3c.py'

# Live checks (stack up): docs/GOTCHAS.md#live-test-scripts
# Static checks safe for the dispatcher /verify (no mutation, no network):
python3 -m py_compile v2/server.py v2/engine.py   # compile-check changed modules
git diff                                          # confirm edits are additive/scoped
```

## Guardrails (NOMAD convention)
- What you may do on your own and what needs the operator is in `docs/SDLC.md` (read it from
  master: `git -C ~/nomad show master:docs/SDLC.md`). Commit on a feature branch freely, merge
  locally once the tests pass and then tell the operator, ask before a push. Never force-push.
- Keep changes scoped; don't add dependencies without justification.
- Secrets only in `.env` (never commit); agents reference LiteLLM **role aliases**
  (deep/balanced/gpt/fast/private/code), never raw model names.
- Irreversible/external actions (send email, push, publish, share, spend, delete)
  must pass the human approval gate — do not bypass it.
- A new project is set up with the `create_project` tool (nomad-create), with the PRD the
  operator approved. Never make its parts by hand: repo, `projects.yaml` entry, board, roadmap,
  stream, mission-control row.
- Diagrams and charts go through `render_diagram`, never matplotlib or an image model. How to
  use it is in `docs/SHARED-RESOURCES.md`.
- v1 (CrewAI in `crew/`, the n8n gate) is being retired; `docs/CUTOVER-RUNBOOK.md` tracks what
  still depends on it. Don't break those paths until the runbook says they are gone.

## Git workflow (hard rule)

Unless the operator says otherwise for a specific piece of work:

- Develop each new feature on its own branch, created from the default branch (`master` or
  `main`) before the first edit. Never build a feature directly on the default branch.
- Merge into the default branch only when every item of the feature is done and all tests pass.
  Tell the operator after the merge. A push needs their OK first.
