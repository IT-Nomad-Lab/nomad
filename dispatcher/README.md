# dispatcher — builder handoff + interactive terminal

The bridge from NOMAD's *brains* to its *hands*: two small native-WSL services that run
**Claude Code inside a target project's repo**. Claude Code auto-reads each repo's `CLAUDE.md`,
so it works with full project context.

Both reuse the same project list: the entries in `nomad/projects.yaml` (found under the first
root that has one, or `NOMAD_REGISTRY`), minus `dispatch: false` entries and folders that do not
exist. That is the whitelist for both services. Without the file they fall back to scanning
`PROJECT_ROOTS` for directories containing a `Nomad.md` marker.

## `dispatch.py` — the Builder dispatcher (HTTP, :8090)

Runs **headless** Claude Code in a repo so an agent (or the console) can implement a task there.

| Endpoint | Body | Purpose |
|---|---|---|
| `POST /dispatch` | `{project, task, mode, branch?, plan?}` | `mode="plan"` (read-only) or `"build"` (edits). `plan` is the approved plan; it goes into the Builder's prompt after `task`. |
| `POST /resolve` | `{project}` | Which registry id `project` names: `{ok, id, name}`, or `{ok: false, reason, error, candidates}`. The engine asks this before the gate. Matching is whole-string only (name, id, alias, exact `owner/repo`); text that merely contains an alias does not resolve. |
| `POST /create-project` | `{name, prd, type?, pitch?, problem?, users?, mvp?, stack?, visibility?, local_only?, autopilot?, new_type?, extra?}` | Set a project up end to end (#1374): forwarded to the engine's `POST /owui/create-project` with the Open WebUI token, where `nomad_projects/create.py` runs the chat's steps in the background. Returns `{ok, id, stage, steps, running, lines}` or the refusal. The MCP tool `create_project` calls this. |
| `GET /create-project?id=` | — | How that run is going (same shape). |
| `POST /new-project` | `{name, …}` | Scaffold a new repo (dir + `Nomad.md` + `CLAUDE.md` + README + `git init`) and append it to `projects.yaml`. One of create's steps; call `/create-project` instead. |
| `POST /verify` | `{project, command}` | Run a **guarded** check in the repo (rc + stdout + stderr). |
| `GET /projects` | — | Registered (dispatchable) projects: `projects` (names), `ids` (registry ids), `detail`. |
| `GET /profiles/list` · `GET /profiles/get?id=` · `GET /profiles/dry-run?id=` | — | The cockpit's Standards area (#1377, `nomad_projects/profile_api.py`), read only: every profile (a bad one with its error), the projects of each type, `pending` (an unmerged edit on `profile/<id>`) and the editor's choices (permission policies with their grants, agent roles, lanes, models, the schema's keys). `get` returns one profile's text. The GET dry run is master's version only (any `yaml` is dropped), for the read-only phone page (#1378). `GET /profiles` stays the + New project type menu. |
| `POST /profiles/validate` · `POST /profiles/dry-run` | `{id, yaml}` · `{id, yaml?}` | Check a profile's text without saving it, with the `diff` against master. Dry run: what a new project of the type gets (interview, `projects.yaml` entry, stack, agents, gates, files, autopilot, policy layers, effective MCP, skill, tool and settings access); with `yaml`, also `changes`, the one-line-per-fact diff of master against the edit (empty when the edit changes nothing a project gets). `keep: true` (the form) diffs the edit with the untouched fields kept as written. |
| `POST /profiles/save` | `{id, yaml, keep?}` | Validate, then commit `profiles/<id>.yaml` on branch `profile/<id>` (#1379), never master: a 400 names the bad field and nothing is written. Returns `branch`, `commit` and the `diff` against master; `unchanged: true` when the text matches the branch (or master), with no commit. `keep: true` keeps the text and comments of the fields the form did not change. Merging the branch is a yellow action. Not allowed from the tailnet. |
| `GET /policies` · `GET /policies/get?id=` · `GET /policies/effective?project=` | — | The policy layer (#1396, `nomad_policy/api.py`): every policy with its role and attachments, one policy's file, a project's effective access with the statement that decided each item. |
| `POST /policies/simulate` · `POST /policies/tag-dry-run` | `{project, kind, name, context?}` · `{project, type}` | Would this call be allowed, and why. What tagging a project with a type would change. |
| `POST /policies/tag-save` · `POST /policies/tag-accept` | `{project, type}` · `{project, change}` | Save a tag on `policy/tag-<project>` (legacy file and `projects.yaml`), only when its diff is empty and the hub is on master; 409 while that branch exists. Accept lifts one held `mcp:` change on the same branch (#1398). `tag-dry-run` returns `saved`, the tag already on a branch or master. |
| `POST /policies/validate` | `{id, yaml}` | Parse and check a policy without saving it (the cockpit editor's YAML to form step, #1397); it returns the `diff` against master, which the guardrail dialog shows (#1398). `GET /policies` also returns the attach targets and the schema's choices, and `get` returns `pending`, the unmerged edit on `policy/<id>`. |
| `POST /policies/save` · `POST /policies/attach` | `{id, yaml}` · `{target, name, policy, detach?}` | Save a policy, or attach or detach one on a type or project, as a commit on a `policy/*` branch. Never on master, never a guardrail (403). |
| `POST /policies/guardrail-save` | `{id, yaml, confirm}` | Save a guardrail on its branch. Needs `X-Nomad-Cockpit` with `~/.config/nomad/cockpit-token` (0600, made on first start, `NOMAD_COCKPIT_TOKEN` overrides) and `confirm` equal to the id. Only the engine sends the header, for the operator in the cockpit; the dispatch token alone is refused, and the hooks deny agents the token file and the route. |
| `POST /comfy/generate` | `{graph, timeout?}` | Run one ComfyUI API-format graph under shared GPU admission (#1462, `comfy_gate.py`). The v2 engine's local image provider calls this instead of :8188, because the engine runs as root in a container and cannot hold `/tmp/nomad-gpu.lock`. It takes admission without waiting (`{ok: false, busy: true}` while `gpu_guard`, another admitted workload or a pending restore holds the GPU), refuses when ComfyUI already has work from outside admission, queues the graph, waits up to `timeout` (default 300 s, max 1800), and returns `{ok, prompt_id, png_b64}`. Before it releases, it stops its own prompt if still there (by `prompt_id`, never anyone else's), waits for ComfyUI to go idle (bounded by `NOMAD_COMFY_SETTLE_SECONDS`, 120 s), calls `/free` and waits up to 30 s for its VRAM to drop; anything left over comes back in `settle`. One generation at a time per dispatcher; a second waits in-process. Deploy order: restart the dispatcher **before** recreating the engine, or the engine's ComfyUI calls get a 404 and an `auto` chain falls through to the paid providers. Tests: `dispatcher/test_comfy_gate.py`. |
| `GET /health` | — | Liveness. |

**Guardrails:**
- **Token-gated.** Every route but `GET /health` requires `X-Nomad-Token`, the same convention the
  n8n webhooks use. The secret is auto-generated at `~/.config/nomad/dispatch-token` (0600) on
  first start, so there is nothing to configure; `NOMAD_DISPATCH_TOKEN` overrides it. Unlike
  `termd`, this does **not** fail open: if the token cannot be read or created the process exits,
  because a dispatcher with no token is a remote-code-edit daemon with no door. Containers read the
  file through a read-only mount (`/term-secret/dispatch-token` in the engine and crew,
  `/host/.config/nomad/dispatch-token` in the console).
- `build` runs with `--permission-mode acceptEdits`: Claude may **edit** files (reversible,
  uncommitted) but is instructed to **never** `git commit`/`push` or run destructive commands.
- **An ambiguous project name refuses.** This service edits code, so resolving "nomad" to whichever
  repo `listdir` happened to return first is a way to silently write into the wrong one. An exact
  name or directory match wins outright; only a tie *within* a tier is a real ambiguity, and that
  returns the candidates instead of guessing.
- `/verify` is **denylist-guarded** (`guard_command`): it only *runs* checks — it blocks
  `rm -rf`, git mutations, docker, sudo/systemctl, kill, external network (localhost curl
  allowed), `/dev` writes, fork bombs. It never mutates or reaches the network.

## `termd.py` — the terminal daemon (WebSocket, :8091)

A real **PTY running interactive `claude`** in a selected repo, streamed to the browser
(xterm.js). This is the operator's own session — they drive it.

| Endpoint | Purpose |
|---|---|
| `WS /term?project=<name>&cols=&rows=` | Attach an interactive `claude` session in the repo. |
| `GET /health` | Liveness. |

- Sessions **persist per project**: closing the tab leaves the session alive; reconnect
  re-attaches with scrollback. Idle sessions are reaped.
- **Token-gated** at the WebSocket handshake (auto-generated `~/.config/nomad/term-token`),
  so another local process can't attach. The console proxies the browser WS here.

## Run it

These run **natively in WSL** (where `claude`, the `~/.claude` login, and the repos live) — not
in containers. Both bind `127.0.0.1` (#821); containers still reach them via
`host.docker.internal`, because they also bind the Docker Engine bridge `172.17.0.1`
(`NOMAD_BRIDGE_IP`, `bridge_bind.py`, #383).

```bash
python3 dispatch.py     # :8090   (Builder dispatcher)
python3 termd.py        # :8091   (terminal daemon; needs the websockets dep)
```

Typically run as systemd user services (`nomad-dispatch`, `nomad-term`) so they survive reboots.

## Key environment variables

| Var | Default | Meaning |
|---|---|---|
| `PROJECT_ROOTS` | `~` | Colon-separated roots scanned for `Nomad.md` projects. A repo outside them (the Tauri shell on the Windows side) is **symlinked in** rather than added as a second root, because a second root that contains another checkout of an existing project creates two repos with one name. |
| `NOMAD_DISPATCH_HOST` / `_PORT` | `127.0.0.1` / `8090` | Dispatcher bind. |
| `NOMAD_DISPATCH_TOKEN` | auto | Shared secret for every route but `/health`; auto-generated at `~/.config/nomad/dispatch-token`. |
| `NOMAD_DISPATCH_TIMEOUT` | `900` | Per-dispatch timeout (s). |
| `NOMAD_TERM_HOST` / `_PORT` | `127.0.0.1` / `8091` | Terminal daemon bind. |
| `NOMAD_TERM_CMD` | `claude` | What the PTY runs. |
| `NOMAD_TERM_TOKEN` | auto | Handshake token (shared with the console). |
