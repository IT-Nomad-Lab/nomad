# NOMAD plugin (Claude Code)

Packages NOMAD v2 as one installable Claude Code plugin: the build-team **agents**, operator
**slash-commands** that drive the running engine, and the lane **MCP servers**. It works on both
hosts, WSL and Windows (#1056).

## What's inside
```
nomad-plugin/
├─ .claude-plugin/plugin.json   # manifest
├─ agents/                      # 7 build-team subagents (pm-orchestrator, architect, …)
├─ commands/                    # /nomad-capture, /nomad-runs, /nomad-approve
├─ skills/agent-docs-audit/     # read-only audit of CLAUDE.md / Nomad.md / memory / worktrees
├─ mcp/                         # the lane MCP servers (comms/research/content/dev)
├─ bin/nomad-run                # finds nomad-operator's launcher, which runs the servers in WSL
└─ .mcp.json                    # MCP server declarations
```

## Install
`claude-hooks/install.sh` installs it with nomad-operator, on both hosts (`install.ps1` on
Windows). See `claude-hooks/README.md`. It needs nomad-operator: the MCP servers run through its
`bin/nomad-run`.

## Use (with the NOMAD v2 stack running)
Bring the stack up first — `docker compose up -d` in `~/nomad` (engine on `localhost:8099`,
NocoDB on `:8095`). Then:

- **`/nomad-capture <goal>`** — push a goal into the pipeline; it routes to a lane and pauses at
  the human gate.
- **`/nomad-runs`** — see recent runs; spot anything `awaiting-approval`.
- **`/nomad-approve <run_id> approve|reject`** — make the gate decision. *Approve runs a real
  action* (email/build) — the engine's app-layer interlock refuses to act on an unapproved run.

## Notes
- **No hook.** The red tier (push, PRs, prod) is nomad-operator's tier gate. The old guard hook
  is gone (#1056); note that the tier gate doesn't block printing `.env`, which the guard did.
- **MCP servers** are the *gated* lane tools (they write to NocoDB only after approval). On both
  hosts they run in WSL as `nomad-run lane-mcp <lane>`, with the `~/nomad/mcp-local/.venv` Python
  and the settings in `~/nomad/.env` (`NOMAD_ENV_FILE`). The dev lane calls the dispatcher on
  `127.0.0.1:8090` (`NOMAD_DISPATCH_URL`). The slash-commands only call the engine API with curl,
  so they need no Python on Windows.
- **Specialist skills** (`comms/research/support/ads/dev`) live in `~/nomad/v2/skills/` and are read
  by the engine at runtime — they are playbooks for the engine, not Claude Code skills, so they're
  not bundled here.
- **`agent-docs-audit`** is a Claude Code skill: it grades CLAUDE.md, AGENTS.md and Nomad.md
  against `references/rubric.md` and runs `scripts/checks.py` (size, stale memory, drift, merged
  worktrees). Read-only. Installed for everyday use as a copy in `~/.claude/skills/agent-docs-audit/`.
