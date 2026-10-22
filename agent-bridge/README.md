# agent-bridge

Open WebUI as a front-end for the local Claude Code (Max login, never the API). One chat turn
can edit files, run shell, launch subagents, call MCP servers and stop at approval gates.

Off by default: the pipe valve `AGENT_ENABLED` is False and the service is not installed
until the operator starts it. The existing chat path is untouched.

| File | Job |
|---|---|
| `server.py` | HTTP (`:8093`, token = `~/.config/nomad/owui-token`), chat commands, background runs, Zulip notice |
| `runner.py` | one `claude -p` stream-json process per turn, `--resume`, control protocol |
| `events.py` | stream events -> chat text, footer with numbered pending list |
| `gates.py` | pending approvals keyed by run id; `approve` with nothing pending says so |
| `policy_hook.py` | PreToolUse: deny `git stash`, commit in shared ~/nomad without `--only`, force-push; ask for push/rm -rf/deploy/... |
| `projects.py` | project entry -> cwd (`general` = ~/nomad; `local_only` refused) |
| `backends.py` | claude / codex / local: availability, cooldown, labels, Codex event translator |
| `store.py` | chat id -> session id, run log (SQLite) |

Chat commands: `!new`, `status`, `stop [id]`, `approve [id]`, `deny [id]`, `!bg <task>`, `!deep <task>`.

Routing: NOMAD Auto picks per turn (work request, existing agent session or pending gate -> here; `!chat`/`!agent` force it). `GET /agent/state` feeds that choice.

Endpoints: `POST /agent/chat`, `/agent/decide`, `/agent/stop`; `GET /agent/attach`, `/agent/pending`, `/agent/runs`, `/health`.

Tests: `python3 -W ignore agent-bridge/test_bridge.py` (fake claude, 53 tests).

Durable asks (#1361): every permission request or question gets a stable `ask_id`
(`ask-<10 hex>`), saved in the `asks` table of `state.db`. `GET /agent/pending?chat_id=&state=`
lists asks in any state: `ask_id, run_id, chat_id, kind (permission|question), tool, summary,
reason, created, state`, plus `questions` (AskUserQuestion shape) for questions. States:
`pending`, `answered`, `timeout` (30 min, denied), `cancelled` (run stopped), `expired`.
- Restart: the claude process that asked dies with the bridge, so every ask still `pending` at
  startup becomes `expired`. It stays listed; deciding it returns
  `{"ok": false, "state": "expired", "error": ...}` and applies nothing.
- `POST /agent/decide {ask_id | run_id, decision, answers?, chat_id?}`. First answer wins: the
  first decision applies, any later one returns `{"ok": false, "error": "already answered"}`.
- `answer <ask_id> <json>` is the chat form, parsed in the bridge (`gates.parse_answer`), so the
  pipe needs no parser: send it as a normal chat message to `/agent/chat`, or raw to
  `POST /agent/answer {text, chat_id?}`. json is `{"decision":"approve"|"deny","note"?}` or
  `{"answers":[{"question","answer"}]}` (answer: string or list). Bad json is an error, never an
  approval.
Smoke against real Claude Code: `python3 -I agent-bridge/smoke.py` (15 checks, ~30 s, uses plan quota).

Install (operator, shared state):
`cp nomad-agent-bridge.service ~/.config/systemd/user/`, add a drop-in with
`Environment=NOMAD_BRIDGE_IP=172.17.0.1`, `systemctl --user enable --now nomad-agent-bridge`,
run `scripts/owui-sync-pipe.py`, then set `AGENT_ENABLED` in the pipe valves.

Fallback: Claude Code first; when it is out of quota/logged out and nothing ran yet, the turn
continues on Codex CLI (sandboxed, can't commit), then the local model if :8240 and
`~/.config/nomad/litellm-key` exist. `!claude` / `!codex` / `!local` force one turn; `status` lists
backends. Each run's footer names the agent and model.

## MCP servers on all three backends

One registry feeds every backend (PRD `docs/prd/mcp-all-backends.md`):

- Sources: `~/nomad/.mcp.json` (project) and the `mcpServers` in `~/.claude.json` (user).
- Claude Code reads both itself. `scripts/mcp-sync.py` writes the rest: a generated block in
  `~/.codex/config.toml` for Codex, and `~/.config/nomad/mcp-all.json` for local runs.
- The bridge runs the sync before every run when a source file changed, so a server installed
  today reaches Codex and local on the next turn, no restart. `status` lists the counts.
- Codex has no approval channel, so the block sets `default_tools_approval_mode = "approve"` for
  every server; otherwise it refuses any tool not marked read-only.
- Local runs start with `ENABLE_TOOL_SEARCH=true` so the 22 servers' schemas stay out of the
  64k prompt (all loaded: ~420k tokens; deferred: ~30k).
- To keep a server away from one backend: `~/.config/nomad/mcp-backends.json`,
  `{"codex": ["name"], "local": ["name"]}`.
- Add a server with `claude mcp add --scope project <name> ...` in `~/nomad`; nothing else.

## File events (#1331, #1355, #1400, #1401)

`{"type":"file","name","mime","size","b64","path"}` is sent when the run makes a file the chat can
show. Besides a successful `Write`, the bridge now finds files made by Bash, MCP tools and
subagents: after each tool result it re-scans the run's cwd (depth 4, 3000 files, 0.4 s; skips
`.git`, `node_modules`, `venv`/`.venv`/`env`, any dir with `pyvenv.cfg`, `__pycache__`, `dist`,
`build`, dot-dirs and dotfiles) and diffs it against the previous snapshot, and it checks absolute
paths named in the tool's input or output that exist, are a known type, were modified during the
run and have no dot-directory in them. Each real path is sent once; at most 10 files per run.

New optional fields (absent on a normal event, so an old consumer sees the old shape):

| field | meaning |
|---|---|
| `download_only: true` | show a link, never an embed. Types outside `FILE_TYPES` that are worth a download: zip, tar, gz, tgz, 7z, json, html, xml, tsv, epub, odt, mov, mkv, flac, m4a, aac, bmp, tiff, ico (`DOWNLOAD_TYPES`). `b64` carries the bytes unless `too_large`. |
| `too_large: true` | the file is over `AGENT_BRIDGE_FILE_MAX_BYTES` (8 MB). `b64` is `""`; only `path`, `size`, `mime` are real. Implies `download_only`. |
| `reason` | one human sentence for why it is flagged. |
| `source` | how it was found: `write`, `scan` or `path`. |

An old pipe that gets a `too_large` event decodes an empty `b64`, falls into its existing
"Couldn't show `x`... It is at `path`" message and does not crash. A `download_only` event with
bytes is uploaded and rendered by mime like any other. Any other unlisted type (`.sh`, `.pyc`, a
log), an empty file, and anything over the per-run cap is still not sent, but it is logged.

Logging (INFO, logger `agent-bridge.files`, visible with `journalctl --user -u nomad-agent-bridge`):
`file event: <path> name= mime= size= source= [flags=...]` for every event and
`file dropped: <path> (<source>): <reason>` for every drop of a path the model named (Write);
guessed paths that are not files are not logged.
