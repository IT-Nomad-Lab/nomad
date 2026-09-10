---
nomad: true
name: NOMAD
status: active
owner: Josias
repo: local
lane: dev
stack: [python, fastapi, uvicorn, docker, postgres, nocodb, qdrant, litellm, n8n]
updated: 2026-09-10
---

# NOMAD — the orchestrator itself

This is NOMAD's own repository. The engineering crew develops and tests NOMAD here.
NOMAD v2 (the explicit Capture→Clarify→Route→Process→Human-Gate→Execute→Log&Learn
pipeline) lives in `v2/` and runs as the `nomad-v2-engine` service on NocoDB/Postgres.

Builder/dev guardrails apply: edits land UNCOMMITTED for human review; never commit
or push automatically. Secrets only in `.env`; agents reference LiteLLM role aliases,
never raw model names.

Authoritative context and resume point: **CLAUDE.md** in this folder.

**Shared resource — NOMAD CRM** (`http://localhost:8400`): NOMAD's grant & funder outreach CRM is available to every project. Use it to follow up on the grants, funders and organizations you contact for **NOMAD** — a pipeline (Identified → Qualifying → Outreach Sent → In Conversation → Application Submitted → Under Review → Awarded), dated follow-ups, contacts, and multi-currency reporting. Filter the CRM to **NOMAD** to see just this project's opportunities. Shared web app — don't modify it here (it lives in the `nomad-crm` repo).

**Shared resource — AI-text detection** (`http://127.0.0.1:8214`, MCP `nomad-detect`): prose written in the JDE style has to clear an AI-detection check before it ships, not just the style rules. Two channels run locally: a statistical detector (Binoculars, two Qwen2.5-1.5B models on the RTX 5090) and a deterministic style linter that names the exact line to change. `POST /detect` runs both, `POST /lint` is the instant no-GPU pass, or call `~/nomad/nomad-detect/check.py` from a shell (exit 0 pass · 1 revise · 2 fail). Known blind spot: the statistical channel cannot separate Claude Opus prose from human writing (AUC 0.367), so on an Opus draft the style score is the one that counts. Owned here: `nomad-detect/`.

**Shared resource — Codex backup brain** (LiteLLM alias `codex`, bridge on `127.0.0.1:8089`): a second agentic CLI (OpenAI Codex, on the ChatGPT subscription) behind an OpenAI-compatible shim, so NOMAD stops depending on one vendor's quota. Until now every subscription-backed call went through the `claude` CLI, and a 5-hour throttle dropped the whole stack to llama3.1:8b. Now the chains fail over through Codex first: `deep → balanced → codex → gpt → fast`, and `code → codex → gpt → fast`. For text there is nothing to call directly — ask for a role alias through the LiteLLM gateway (`http://localhost:4000`) and the routing handles it. **Images are the exception: ask for Codex by name.** The MCP tool `codex_image(prompt, subdir, filename)` renders through Codex's own image generation and saves into this repo. It is deliberately kept OUT of the `generate_image` fallback chain because it spends ChatGPT subscription quota — local SDXL stays the default, and Codex is the deliberate choice when prompt adherence matters (it holds detail SDXL drops, and renders legible text). Owned here: `codex-bridge/`.

> Managed by **NOMAD**. Companion files: **CLAUDE.md** (context) · **AGENTS.md** (agent build/test rules).
