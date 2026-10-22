# Autopilot rules for workers

Signed off by Josias on 2026-10-01 (task #876).
Source: PRD `docs/prd/autopilot.md` 5.2 to 5.8, generalised from `RuneMatch/AUTONOMY.md`.

Josias owns this file. It gives autopilot workers standing authority to finish board work
without asking him each time. It applies only to runs the `nomad-autopilot` supervisor starts.
Where it conflicts with `CLAUDE.md` or `AGENTS.md` about committing, it wins **for autopilot runs
only**: you commit on your task branch without being asked. Every other guardrail in those files
still holds. The project's own `AUTONOMY.md`, if there is one, adds restrictions on top of this
file. When the two disagree, the stricter rule wins.

You are one worker. You get one task, one worktree, one branch. The supervisor picks tasks,
merges, alerts and posts digests. You don't.

## 1. The cycle

0. **Check the shell.** Your first shell command is `git status` in your worktree. See section 5.
1. **Read.** `get` the task. Read its acceptance criteria, its parent, and the last comments. A
   comment `autopilot: resume` means an earlier run left work on this branch; start from what it
   says is left. A task in **Approved** or **Changes requested** is the operator's answer to your
   question (section 6); your prompt quotes it.
2. **Promote, if needed.** If the task is New, move it to Ready with the comment
   `autopilot: promoted`, but only when all of these hold:
   - it has acceptance criteria;
   - it has no open `blocked_by`;
   - it is a Task, Bug or Sub-task;
   - it isn't in the `manager` lane and isn't tagged `human`;
   - meeting its criteria needs no reserved decision (section 2).

   If it lacks acceptance criteria, draft them from the description, post them as a comment and
   log that as a delegated decision (section 3). If drafting them would itself be a reserved
   decision, go to section 6 instead.
3. **Claim.** Move it to In Progress with the comment `autopilot: started <branch>`. This works
   from Ready, Approved and Changes requested.
4. **Work where you were put.** The supervisor made your worktree and branch. Stay in them. Don't
   create or switch branches, and don't open another worktree. Delete a branch or worktree only
   with an Approved answer that names it (section 6).
5. **Build to the acceptance criteria.** Nothing more. Commit on your branch as you go, small
   commits, each ending with the `Co-Authored-By:` trailer your run gives you. Anything you find
   outside the task becomes a new task (section 2), not extra scope.
6. **Test.** Section 4.
7. **Report a result.** Section 7. Then stop.

**Out of time.** Commit what you have, comment `autopilot: resume` with what is done and what is
left, and stop. The next run picks it up.

## 2. Decide and log, or stop and ask

**You decide, and log each decision (section 3):**
- tuning numbers: balance values, timings, thresholds, limits, scores, layout sizes;
- wording of UI text, hints, messages, docs and comments, as long as it changes no name, canon
  or public claim;
- test fixes, flake handling and moving tests between suites;
- splitting your task into Sub-tasks, and opening follow-up tasks for bugs or gaps you find.
  Create them as New, under the right parent, with acceptance criteria and `blocked_by` where
  order matters. Don't work them in this run;
- refactors that keep the existing architecture (module boundaries, layering, namespaces, public
  interfaces);
- assets made in code or in the engine: primitives, materials, shaders, particles, procedural
  meshes, CSS, hand-written SVG, and reuse of assets already in the repo;
- a new dependency under MIT, BSD, Apache-2.0 or ISC that costs nothing and calls no external
  service.

**Reserved for Josias:**
- story canon and naming: characters, places, product and brand names, lore, anything a reader
  takes as fact about the world or the business;
- art direction, and any generated image, model, audio or video that ships;
- money, licences (any other than those listed above), external services and accounts;
- push, rewriting history, and any force operation;
- deleting branches, worktrees or tracker items, and merging into the default branch. Ask first
  (section 6). An Approved answer that names the branch, worktree, item or merge lets you do that
  one thing in the next run, and nothing else;
- edits to agent config: `AGENTS.md`, `CLAUDE.md`, any `AUTONOMY.md`, anything in `.claude/`,
  the project's `autopilot:` entry in `projects.yaml`, and permission settings. **Exception:**
  when Josias asked for that edit, in the task description or in a comment Josias wrote on the
  task, make it and log it as a delegated decision (section 3), quoting the request;
- architecture changes: new or merged modules, changed layering or namespaces, a changed public
  interface other code depends on, a new framework or datastore, a schema change that drops data;
- publishing, deploying, or sending anything outside this machine;
- anything irreversible not listed here.

Not sure which list it's on? It's reserved.

## 3. Logging a delegated decision

Every decision from the first list gets two records:
1. A task comment starting `Delegated decision:`, saying what you chose, why, and the commit.
2. One line appended to `docs/DECISIONS.md` in the repo, committed on your branch:
   `- <YYYY-MM-DD> #<task id> <what you chose> (<why>)`. Create the file if it's missing, with
   the heading `# Delegated decisions`.

Josias reviews these in the daily digest and reverses them with a task. A decision he can't find
is one he can't reverse.

## 4. Test gate

Run tests **only** with the commands the project declares in its `autopilot:` entry in
`projects.yaml` (restated in the project's `AUTONOMY.md`). Run them exactly as declared, lock
wrapper included. Never invent a test command, and never edit the declared one. If none is
declared, report *needs operator*.

| When | What runs | Pass means |
|---|---|---|
| While building | the declared fast check (compile, lint) plus the tests your change touches | clean, and touched tests green |
| Before reporting *ready to merge* | the full declared pre-merge gate, once | the baseline rules below |

The supervisor reruns the full gate at merge time and runs the milestone gate when a Feature
completes. You don't run either of those.

**Baseline rules:**
- Every failure is either a known failure or in the declared flake set.
- A flake counts as passing only after it passes **3 solo reruns in a row**.
- A flake that fails that way in two separate runs becomes a new task, `stabilise <test>`. You
  don't add tests to the flake set yourself.
- Test counts only go up. The exception is a task that deletes tests on purpose, and says so in
  its result.
- Restore any files the project says the tests dirty (screenshots, generated fonts) before you
  commit.
- Put the test table (suite, passed, failed, skipped, count before and after) in your result.

If the same test fails the same way **3 times** after fixes, stop and report *failed*.

GPU-heavy steps (image generation, TRELLIS) and any lock the project declares: take the lock the
declared way, every time, even if nothing else seems to be running.

## 5. Shell

- **One simple command per shell call.** No `;`, `&&`, `||`, pipes, redirects, `$( )`, `bash -c`
  or `cd x &&`. Use `git -C <worktree> ...` and absolute paths instead of `cd`.
- Prefer the Read, Grep, Glob and Edit tools over `cat`, `grep`, `find` and `sed`.
- A refused command means **that command** was refused. It doesn't mean the shell is gone. Retry
  it once in a simpler form: split it, drop the pipe, or use a dedicated tool.
- Only a refused plain `git status` in your worktree means the shell is unavailable. Then report
  *failed* with reason `shell unavailable` and stop.
- List every refused command, verbatim, in your result.
- Stop anything you started in the background before you report.

The RuneMatch trial lost whole runs to this: one chained command was refused, the agent decided
the shell was denied, and quit with the work undone.

## 6. When you're stuck

- **Needs a reserved decision, or a fact only Josias has:** move the task to Awaiting approval
  with the exact question and the options you see, commit what you have, and stop. Only the
  operator can answer it: they move the task to **Approved** or **Changes requested** (from
  OpenProject, the cockpit, or a comment starting `approve` or `reject`). The next run gets their
  answer in its prompt. Approved means go ahead, following the answer. Changes requested means the
  answer is your new instruction. Never move a task out of Awaiting approval yourself, except to
  Blocked or Won't do when the operator says so in a comment.
- **Waits on other work:** `relate` it as `blocked_by` that task, comment `autopilot: resume`
  with what you're waiting for, commit what you have, and stop. It stays In Progress; the
  supervisor resumes it once the blocker closes.
- **An error you can't fix:** report *failed* with what you tried.

Never wait, poll, or pick up another task. The supervisor schedules what runs next.

## 7. Result

End every run with one task comment that starts `autopilot: result <outcome>`, where outcome is
one of:

| Outcome | Task status you leave | The comment carries |
|---|---|---|
| `ready to merge` | In Review | commits, test table, delegated decisions, refused commands |
| `resume` | In Progress | what's done, what's left, refused commands |
| `needs operator` | Awaiting approval | the question, the options, refused commands |
| `failed` | Blocked | the error, what you tried, failing tests, refused commands |

Never move a task to Done. The supervisor does that after the merge.

## 8. Never

- push, or touch any remote;
- stash, reset, rebase, clean, amend, or anything with `--force`;
- delete tags, or files your task doesn't own;
- delete branches, worktrees or tracker items, or merge into the default branch, without an
  Approved answer that names it (section 2);
- check out the default branch;
- edit agent config that Josias didn't ask for (section 2);
- write anything in the main checkout. You may read it;
- post a digest or send an alert;
- work on a second task in the same run.
