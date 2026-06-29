# NOMAD plugin (Claude Code)

Packages NOMAD v2 as one installable Claude Code plugin: the build-team **agents**, the
irreversible-action **guard hook**, operator **slash-commands** that drive the running engine, and
the lane **MCP servers**.

## What's inside
```
nomad-plugin/
├─ .claude-plugin/plugin.json   # manifest
├─ agents/                      # 7 build-team subagents (pm-orchestrator, architect, …)
├─ commands/                    # /nomad-capture, /nomad-runs, /nomad-approve
├─ hooks/                       # guard.py + hooks.json (PreToolUse: gate irreversible actions)
├─ mcp/                         # the lane MCP servers (comms/research/content/dev)
└─ .mcp.json                    # MCP server declarations
```

## Install
```
/plugin marketplace add /path/to/nomad        # this repo as a local marketplace
/plugin install nomad
```
(or copy `nomad-plugin/` into a marketplace you control). Restart Claude Code to load it.

## Use (with the NOMAD v2 stack running)
Bring the stack up first — `docker compose up -d` in `~/nomad` (engine on `localhost:8099`,
NocoDB on `:8095`). Then:

- **`/nomad-capture <goal>`** — push a goal into the pipeline; it routes to a lane and pauses at
  the human gate.
- **`/nomad-runs`** — see recent runs; spot anything `awaiting-approval`.
- **`/nomad-approve <run_id> approve|reject`** — make the gate decision. *Approve runs a real
  action* (email/build) — the engine's app-layer interlock refuses to act on an unapproved run.

## Notes
- **Guard hook** travels with the plugin: it routes irreversible Bash (push/merge/deploy/delete/
  send-email) to *ask* and blocks secret exposure (`cat .env`, token prints).
- **MCP servers** are the *gated* lane tools (they write to NocoDB only after approval). They need
  the v2 environment (`NC_API_TOKEN`, etc. — see `~/nomad/.env`) and Python deps (`mcp`, `requests`).
  If you don't run the v2 stack locally, disable them in `.mcp.json` — the slash-commands (which
  just talk to the engine API) are the primary operator interface.
- **Specialist skills** (`comms/research/support/ads/dev`) live in `~/nomad/v2/skills/` and are read
  by the engine at runtime — they are playbooks for the engine, not Claude Code skills, so they're
  not bundled here.
