# Autopilot permission profile

DRAFT for operator review (task #876). Built in #875. PRD `docs/prd/autopilot.md` 5.8.

Every worker runs headless Claude Code with a settings file generated for its task:

    claude -p ... --settings <state>/settings/<project>-<task>.json --setting-sources "" \
        --permission-mode dontAsk

- `permissions.template.json` is the shared template. You review it once.
- `permissions.py` renders it per worker, from the project's `autopilot:` entry in `projects.yaml`.
  It writes the settings file and the project's wrapper scripts under
  `~/.local/state/nomad-autopilot/`. Nothing is installed until the supervisor calls it.
- `dontAsk` means anything without an allow rule is refused, silently. Deny rules beat allow rules,
  whatever the source.
- `--setting-sources ""` loads only this file. It matters: your `~/.claude/settings.json` allows
  `git -C:*`, `git push:*` and `docker compose:*`, and a repo's `.claude/settings*.json` can add
  allows too. MCP servers (nomad-pm) come from `~/.claude.json` and still load.

## Allowed

| What | Rules | Why |
|---|---|---|
| Read and search | `Read`, `Glob`, `Grep`, `Agent` | Workers read anything, and may use subagents (they inherit this profile). |
| Tracker | `mcp__nomad-pm` | Claim, comment, relate, report. The server has no delete tool. |
| Edits | `Edit(//<worktree>/**)` | Only inside the task's worktree. `Edit` covers Write, MultiEdit and NotebookEdit. |
| Git, read-only | `git status/diff/log/show/rev-parse`, `git branch` (list forms), `git worktree list`, and the same through `git -C <main checkout>` | Looking around. |
| Git, writes | `git -C <worktree> add/commit/switch/merge` | Only in the `-C <worktree>` form. See the note below. |
| Shell | `cd ls test cat head tail grep find date wc echo pwd mkdir` | Plain utilities. `find -exec`/`-delete` are not covered by `find *`. |
| Project commands | `Bash(<state>/bin/<project>-<name>)`, plus `... *` if it takes a test filter | One wrapper per command (below). Nothing else of the project's is allowed. |

**Why git writes need `-C <worktree>`.** A rule can't see the shell's current folder. With plain
`git commit` allowed, `cd <main checkout>` followed by `git commit` would commit on the default
branch. The `-C <worktree>` form can only touch the worktree. `AUTONOMY.md` section 5 already tells
workers to use `git -C <worktree>`.

## Denied

Denies are a second line: none of these has an allow rule, so `dontAsk` refuses them anyway. They
stay in so that a mistake in the allow list, or another settings source, can't open them.

- **Git:** push, stash, reset, rebase, clean, `branch -D/-d/--delete`, `commit --amend`,
  `worktree remove`. Each is denied in the plain form, the `-C <worktree>` form and the
  `-C <main checkout>` form, because a deny on `git push *` does not catch `git -C x push`. Force
  forms of `switch` (`-f`, `--force`, `-C`, `--discard-changes`). On the main checkout also
  `checkout`, `commit` and `merge`: the supervisor does the fast-forward, not the agent.
- **Deletion:** `rm -rf`, `rm -fr`, `rm -r`, `rm -R`, `rm --recursive`. Plain `rm` isn't allowed.
- **Edits to agent config:** `AGENTS.md`, `CLAUDE.md`, `AUTONOMY.md`, `.claude/**`, `.mcp.json`,
  `projects.yaml`, anywhere on disk; `~/.claude/**`.
- **Edits outside the worktree:** the main checkout (read-only), each `working_dirs` folder
  (read-only), and the autopilot state folder, which holds the settings file and the wrappers.
- **Per project:** its `deny_edit_globs`, e.g. RuneMatch's `**/*.unity`, `**/*.prefab`.

## Folders

`additionalDirectories` = the worktree, the main checkout, the folder of each lock, and the
project's `working_dirs`. The trial showed a missing folder refuses everything that touches it,
silently. Only the worktree is editable.

## Wrappers for project commands

The trial's `Bash(flock /tmp/runematch-playmode.lock:*)` never matched: Claude Code doesn't strip
`flock` or `VAR=x` prefixes for allow rules, and allowing `flock *` would allow any command. So
each command a worker may run (every test suite, each pre-merge gate step, the build, each
`post_test` cleanup) becomes a script, e.g. for RuneMatch:

| Wrapper | Runs |
|---|---|
| `runematch-test-EditMode [filter]` | `scripts/unity.sh test EditMode {filter}` |
| `runematch-test-PlayMode [filter]` | the same for PlayMode, `TIMEOUT=2400`, holding `/tmp/runematch-playmode.lock` |
| `runematch-gate-1` | `scripts/unity.sh compile` |
| `runematch-post-test-1` | `git checkout -- Assets/Tests/Screenshots/` |

Each wrapper:
- refuses to run unless the current git top level is under the project's `worktree_root`, then
  runs from that top level;
- takes one optional filter, only if the command has `{filter}`. The filter must match
  `[A-Za-z0-9_.,:+=/-]`, at most 200 characters, not starting with `-`. Anything else exits 64
  before the command runs. It is passed as one quoted argument;
- exports `env` values shell-quoted, and takes its locks with `flock` on file descriptors, in
  sorted order so two wrappers can't deadlock. `requires_gpu: true` adds `/tmp/nomad-gpu.lock` to
  test and build wrappers;
- is written mode 555, and the state folder is edit-denied.

The milestone gate gets no wrappers. The supervisor runs it.

## A project entry can only narrow

`build_settings` rejects a project entry, with a `ConfigError` naming the field, when:
- a command runs something the template denies. Each part of a `&&`/`|`/`;` chain is checked,
  after stripping `VAR=x`, `timeout`, `env`, `nice` and `git -C/-c` options;
- a command uses `sh -c`, `bash -c`, `eval`, `sudo`, `su` or `xargs`, which would hide a command
  from that check;
- `{filter}` sits inside quotes;
- a working dir is `/`, the home folder or above it, or holds agent config or secrets
  (`~/.claude`, `~/.config`, `~/.ssh`, `~/.gnupg`, the autopilot state folder);
- a path has spaces, globs or parentheses, or a `deny_edit_globs` entry starts with `!` or holds
  `(`, `)` or whitespace;
- the worktree isn't under `worktree_root`, or sits inside the main checkout.

Then `check_narrowing` checks the result: every allow rule is a template rule or a wrapper rule,
every template deny is still there, `defaultMode` is unchanged, and there are no other keys.

## Verifying it

`autopilot/test_permissions.py` covers the RuneMatch snapshot (`testdata/runematch-settings.json`),
about 60 commands and 18 edit paths through a local evaluator, the narrowing checks, and the
wrappers run for real (quoting, bad filters, env, lock). The local evaluator is an approximation
of Claude Code's matcher. The authority is the real CLI, which one opt-in test runs: a haiku
session in a throwaway repo, under a generated profile, tries 13 commands and 4 file writes, and
the test reads Claude Code's own `permission_denials`:

    AUTOPILOT_CLAUDE_E2E=1 uv run --no-project --with pytest --with pyyaml \
        python -m pytest autopilot/test_permissions.py -q -k enforces

It passed on 2026-09-28 with Claude Code 2.1.282. It shows push, stash, reset, amend (which
matches the commit allow), the raw `flock` line, `echo > CLAUDE.md`, and writes to `AGENTS.md`,
`.claude/` and the main checkout refused. It also shows your user-level allows (`git -C:*`,
`docker compose:*`) don't leak in, and the wrappers and `git -C <worktree>` run. Rerun it after a
Claude Code upgrade.

## Open points for the review

1. **The profile is not a sandbox.** A wrapper runs repo scripts (`scripts/unity.sh`) that the
   worker can edit, so a worker can run anything it writes into one. It stops accidents and
   habits, not a hostile agent. The stronger fix is Claude Code's OS sandbox, or having only the
   supervisor run gates. Neither is in this task.
2. **Reads are unrestricted,** `~/.ssh` and `.env` files included. `Read(...)` denies could close
   that. Not added, since the PRD allows read tools.
3. **`git -C <worktree> switch` is allowed** because PRD 5.8 lists it, but `AUTONOMY.md` section 1
   says workers don't switch branches. Pick one. Dropping the rule is the narrower choice.
4. **The GPU lock path** `/tmp/nomad-gpu.lock` is new. Nothing else takes it yet, so the image
   services must use the same file before `requires_gpu` protects anything.
5. **False-positive denies** on commit messages: `git -C <wt> commit -m "... --amend ..."` is
   refused. It's rare and the worker can reword.
