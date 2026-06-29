---
nomad: true
name: NOMAD
status: active
owner: Josias
repo: local
lane: dev
stack: [python, fastapi, uvicorn, docker, postgres, nocodb, qdrant, litellm, n8n]
updated: 2026-06-24
---

# NOMAD — the orchestrator itself

This is NOMAD's own repository. The engineering crew develops and tests NOMAD here.
NOMAD v2 (the explicit Capture→Clarify→Route→Process→Human-Gate→Execute→Log&Learn
pipeline) lives in `v2/` and runs as the `nomad-v2-engine` service on NocoDB/Postgres.

Builder/dev guardrails apply: edits land UNCOMMITTED for human review; never commit
or push automatically. Secrets only in `.env`; agents reference LiteLLM role aliases,
never raw model names.

Authoritative context and resume point: **CLAUDE.md** in this folder.

> Managed by **NOMAD**. Companion files: **CLAUDE.md** (context) · **AGENTS.md** (agent build/test rules).
