# claude-hooks

User-level Claude Code hooks, kept here so they are versioned and tested. Sessions run them
through the `nomad-operator` plugin (`nomad-operator/bin/nomad-run`), on WSL and on Windows.
Agents don't install them: that is a red-tier action in `docs/SDLC.md`, so the operator runs the
install.

## Install (#1054)

| host | run |
|---|---|
| WSL | `~/nomad/claude-hooks/install.sh` |
| Windows | `powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu-24.04\home\<you>\nomad\claude-hooks\install.ps1` |

Add `--dry-run` first to see the settings diff without changing anything. Run WSL first: every
hook runs in WSL, from the WSL export. The installer:

1. exports **master** of `~/nomad` with `git archive` to `~/.local/share/nomad-marketplace`
   (Windows: refreshes that, then copies it to `%USERPROFILE%\.nomad\marketplace`). The branch
   `~/nomad` happens to be on never matters.
2. adds that folder as `nomad-marketplace` and installs `nomad-operator` at user scope, or
   updates both.
3. runs `claude-config/sync_settings.py`, which merges the shared settings and removes the old
   `install.sh` and `ntfy.ps1` hook entries the plugin replaces (settings backed up first).
4. moves the old copies in `~/.claude/hooks/` to `~/.claude/backups/<time>/`.
5. smoke tests the installed plugin and prints what's left by hand: the penpot MCP line (your
   token, never committed) and, on Windows, a `CLAUDE.md` line that still names `ntfy.ps1`.

Under Git Bash, `nomad-run` sets `MSYS_NO_PATHCONV=1` before calling `wsl.exe`, or Git Bash
would rewrite `/home/...` arguments into Windows paths; `docs/GOTCHAS.md` has the rest of the
Windows traps, and `docs/SHARED-RESOURCES.md` how a Windows session reaches NOMAD.

Re-run it after a merge to master to pick the change up. Sessions run the plugin from the export folder.
Claude Code also keeps a copy in `plugins/cache/`; if that copy is ever used, the launcher still
takes the code from the WSL export.

## tier-gate (#1005)

Enforces the SDLC red tier in every interactive session, whatever directory it starts in.

| result | when |
|---|---|
| deny | a force push, `+refspec`, `--mirror`, or deleting a remote branch, in any form |
| ask | `git push` (including `git -C <dir> push`), `gh pr create/merge`, `gh release`, `gh api` writes, package publishes, `docker push`, Docker volume deletes, recursive `rm` outside `/tmp`, `DROP`/`TRUNCATE`/`DELETE FROM`, funnels and tunnels, `publish.sh`, terraform/kubectl writes, `crontab -r`, anything aimed at a `prod`/`production` host, and the MCP tools that send, share or invite |
| nothing | everything else, including commits and local merges (green and yellow) |

Replayed against 2,338 Bash commands from past transcripts, it would have prompted 13 times, and
each of those was a real push or delete.

The autopilot doesn't use it. Its workers run with `--setting-sources ""` and their own
per-task permissions (`autopilot/README-permissions.md`). The repo guard
(`.claude/hooks/guard.py`) still runs in `~/nomad` sessions, for the secret scan and to protect
the gate's own files.

Tests: `uv run --no-project --with pytest python -m pytest -q claude-hooks`

## track.py: nomad-pm tracking (#1006)

SDLC.md asks for a status comment before every session ends. The harness now writes it.

| event | mode | what it does |
|---|---|---|
| SessionStart | `start` | Branch `<type>/<id>-<slug>`: puts that task's status line in the agent's context. Any other feature branch: a one-line naming reminder. Default branch or no repo: silent. |
| PostToolUse (Bash) | `record` | After `git commit`, `merge`, `pull`, `cherry-pick` or `revert` (follows `cd` and `-C`), appends repo, branch and HEAD to `~/.local/state/nomad-track/<session>.jsonl`. No tracker call. |
| SessionEnd | `end` | Groups the session's commits by task (`#N` in the subject, else the branch id) and posts one comment per task that is still open. Done and unknown tasks are skipped. Clears the state file. |

Every error is swallowed and the hook always exits 0: a tracker outage never blocks a session.
It runs under `~/nomad/mcp-local/.venv` to import `nomad_pm`, so comments post as `NOMAD_PM_AS`
(default `nomad-dev`). The agent still owns status changes; the hook only comments.

Tests: `uvx pytest -q test_track.py test_tier_gate.py`.
