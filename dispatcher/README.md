# dispatcher — builder handoff + interactive terminal

The bridge from NOMAD's *brains* to its *hands*: two small native-WSL services that run
**Claude Code inside a target project's repo**. Claude Code auto-reads each repo's `CLAUDE.md`,
so it works with full project context.

Both reuse the same project discovery: any directory under `PROJECT_ROOTS` containing a
`Nomad.md` marker is a NOMAD project (the whitelist for both services).

## `dispatch.py` — the Builder dispatcher (HTTP, :8090)

Runs **headless** Claude Code in a repo so an agent (or the console) can implement a task there.

| Endpoint | Body | Purpose |
|---|---|---|
| `POST /dispatch` | `{project, task, mode}` | `mode="plan"` (read-only) or `"build"` (edits). |
| `POST /new-project` | `{name, …}` | Scaffold a new repo (dir + `Nomad.md` + `CLAUDE.md` + README + `git init`). |
| `POST /verify` | `{project, command}` | Run a **guarded** check in the repo (rc + stdout + stderr). |
| `GET /projects` | — | Discovered `Nomad.md` projects. |
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
  closing the `0.0.0.0` LAN bypass. The console proxies the browser WS here.

## Run it

These run **natively in WSL** (where `claude`, the `~/.claude` login, and the repos live) — not
in containers. The console reaches them via `host.docker.internal`.

```bash
python3 dispatch.py     # :8090   (Builder dispatcher)
python3 termd.py        # :8091   (terminal daemon; needs the websockets dep)
```

Typically run as systemd user services (`nomad-dispatch`, `nomad-term`) so they survive reboots.

## Key environment variables

| Var | Default | Meaning |
|---|---|---|
| `PROJECT_ROOTS` | `~` | Colon-separated roots scanned for `Nomad.md` projects. A repo outside them (the Tauri shell on the Windows side) is **symlinked in** rather than added as a second root, because a second root that contains another checkout of an existing project creates two repos with one name. |
| `NOMAD_DISPATCH_HOST` / `_PORT` | `0.0.0.0` / `8090` | Dispatcher bind. |
| `NOMAD_DISPATCH_TOKEN` | auto | Shared secret for every route but `/health`; auto-generated at `~/.config/nomad/dispatch-token`. |
| `NOMAD_DISPATCH_TIMEOUT` | `900` | Per-dispatch timeout (s). |
| `NOMAD_TERM_PORT` | `8091` | Terminal daemon port. |
| `NOMAD_TERM_CMD` | `claude` | What the PTY runs. |
| `NOMAD_TERM_TOKEN` | auto | Handshake token (shared with the console). |
