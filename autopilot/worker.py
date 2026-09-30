"""Autopilot workers: one task = one worktree = one headless Claude Code run (PRD 5.3, #866).

The supervisor's Scheduler hooks `start_workers`, `collect` and `stop` delegate to a `WorkerPool`:

    pool = WorkerPool()                      # projects come from projects.yaml
    started  = pool.start_workers(ctx, tasks)   # non-blocking; one `claude -p` per task
    finished = pool.collect(ctx)                # reaps exited or timed-out runs
    pool.stop(ctx, "kill")                      # or "finish"

A run is `claude -p <prompt> --settings <file> --setting-sources "" --permission-mode dontAsk
--verbose --output-format stream-json` (permissions.cli_args), started in the task's worktree in
its own process group, with ANTHROPIC_API_KEY blanked so it bills the subscription (as
dispatcher/dispatch.py does). The settings file and the project's command wrappers come from
permissions.write (#875); the prompt names the wrappers, the only way a worker may run a
project command. Its stdout goes to ~/.local/state/nomad-autopilot/runs/<project>/<task>-<ts>.jsonl,
stderr next to it as .stderr, and a run record (.run.json: pid, process start time, paths,
deadline; `collected` once reaped) lets a restarted supervisor find it again (`recover`).

`collect` reads the final stream-json `result` event (result text, total_cost_usd,
permission_denials) and finds the `autopilot: result <outcome>` marker the shared rules
(AUTONOMY.template.md section 7) make every run leave. Outcomes returned to the supervisor:

    merge            "ready to merge"
    resume           "resume", the time limit ran out, or the operator killed the run
    needs_operator   "needs operator"
    failed           "failed", no marker, or the run crashed
    error            the run's first shell command was refused and no later one got through
                     (the RuneMatch trial failure: a bad permission profile, not a bad task)

Workers never merge; the merge queue (#871) does.

Run the tests: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot -q
"""
import glob
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

try:
    from . import projects as projects_mod
except ImportError:  # imported as a top-level module (tests, scripts)
    import projects as projects_mod

log = logging.getLogger("autopilot.worker")

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE = os.environ.get("NOMAD_AUTOPILOT_CLAUDE") or shutil.which("claude") or \
    os.path.expanduser("~/.local/bin/claude")
STATE_DIR = os.path.expanduser(os.environ.get("NOMAD_AUTOPILOT_STATE", "~/.local/state/nomad-autopilot"))
RUN_SECS = float(os.environ.get("NOMAD_AUTOPILOT_RUN_SECS", str(3 * 3600)))
KILL_GRACE_SECS = 10
RULES = os.path.expanduser(os.environ.get("NOMAD_AUTOPILOT_RULES", "~/nomad/docs/AUTONOMY.md"))
RULES_DRAFT = os.path.join(NOMAD, "autopilot", "AUTONOMY.template.md")
SHELL_TOOLS = ("Bash",)

OUTCOMES = {"ready to merge": "merge", "resume": "resume",
            "needs operator": "needs_operator", "failed": "failed"}
MARKER = re.compile(r"autopilot:\s*result\s*[:\-]?\s*\**\s*(ready to merge|resume|needs operator|failed)",
                    re.IGNORECASE)


class WorkerError(RuntimeError):
    """A worker could not be started (bad repo, branch held elsewhere, no rules file, ...)."""


# ---- git and worktrees ------------------------------------------------------------------------

def git(path, *args, check=False):
    p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=60)
    if check and p.returncode != 0:
        raise WorkerError(f"git {' '.join(args)} failed in {path}: {p.stderr.strip()[:300]}")
    return p.stdout.strip() if p.returncode == 0 else ""


def default_branch(repo):
    """origin's HEAD if known, else main or master. Same rule as dispatcher/dispatch.py."""
    head = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head.split("/", 1)[-1]
    for b in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}"):
            return b
    raise WorkerError(f"{repo}: no default branch (no origin/HEAD, main or master)")


def slugify(title, limit=40):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:limit].strip("-") or "task"


def task_branch(task_id, title):
    return f"feat/{task_id}-{slugify(title)}"


def existing_task_branch(repo, task_id):
    """A branch an earlier run left for this task (`feat/<id>-*`), whatever its slug."""
    out = git(repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/feat/{task_id}-*")
    names = [n for n in out.splitlines() if n]
    return sorted(names)[0] if names else None


def worktree_holding(repo, branch):
    """The worktree that has `branch` checked out, or None."""
    path = None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def is_dirty(path):
    return bool(git(path, "status", "--porcelain"))


def reset_regenerated(path, regenerated):
    """Reset the project's `regenerated` paths (files its tools rewrite on their own, such as a
    Unity dynamic font atlas) to HEAD in the worktree at `path`, index and working tree. Only
    tracked changes under those paths; anything else, untracked files included, is left alone.
    Returns the paths that were reset."""
    reset = []
    for p in regenerated:
        if git(path, "status", "--porcelain", "--untracked-files=no", "--", p):
            git(path, "restore", "--source=HEAD", "--staged", "--worktree", "--", p, check=True)
            reset.append(p)
    if reset:
        log.info("%s: reset regenerated %s", path, ", ".join(reset))
    return reset


@dataclass
class Worktree:
    path: str
    branch: str
    base: str
    created: bool          # a new worktree was added this time
    new_branch: bool       # the branch was created this time (False: a resume)
    dirty: bool            # an existing worktree held uncommitted changes; kept as is


def ensure_worktree(repo, root, task_id, title="", regenerated=()):
    """`<root>/ap-<id>` on `feat/<id>-<slug>` from the default branch.

    - The worktree already exists: keep it exactly as it is (dirty or not) and report.
    - The branch exists (an earlier run's): check it out in a new worktree, a resume.
    - Neither: new branch from the default branch.
    Never resets, stashes, cleans or deletes anything, with one exception: before an existing
    worktree's dirty check, the project's `regenerated` paths are reset to HEAD (see
    reset_regenerated). Those are tool output nobody edits by hand."""
    if git(repo, "rev-parse", "--is-inside-work-tree") != "true":
        raise WorkerError(f"{repo} is not a git repository")
    base = default_branch(repo)
    path = os.path.join(root, f"ap-{task_id}")
    if os.path.exists(path):
        if git(path, "rev-parse", "--is-inside-work-tree") != "true":
            raise WorkerError(f"{path} exists but is not a git worktree; left alone")
        branch = git(path, "branch", "--show-current")
        if not branch:
            raise WorkerError(f"{path} is on a detached HEAD; left alone")
        reset_regenerated(path, regenerated)
        return Worktree(path, branch, base, created=False, new_branch=False, dirty=is_dirty(path))
    os.makedirs(root, exist_ok=True)
    branch = existing_task_branch(repo, task_id)
    if branch:
        held = worktree_holding(repo, branch)
        if held:
            # Another worktree (a manual lane, an older runner) has the branch checked out, and
            # git allows one checkout per branch. Adopt it when it is clean; never touch a dirty
            # one, somebody may be working there.
            if os.path.realpath(held) == os.path.realpath(repo):
                raise WorkerError(f"{branch} is checked out in the main checkout {repo}; "
                                  "switch it back to the default branch to let the autopilot resume")
            reset_regenerated(held, regenerated)
            if is_dirty(held):
                raise WorkerError(f"{branch} is checked out in {held} with uncommitted changes; "
                                  "commit or free that worktree to let the autopilot resume")
            return Worktree(held, branch, base, created=False, new_branch=False, dirty=False)
        git(repo, "worktree", "add", path, branch, check=True)
        return Worktree(path, branch, base, created=True, new_branch=False, dirty=False)
    branch = task_branch(task_id, title)
    git(repo, "worktree", "add", "-b", branch, path, base, check=True)
    return Worktree(path, branch, base, created=True, new_branch=True, dirty=False)


# ---- settings (#875) -------------------------------------------------------------------------

def _permissions():
    """autopilot.permissions, whether this file was imported as part of the package or alone."""
    try:
        from . import permissions
    except ImportError:
        if NOMAD not in sys.path:
            sys.path.insert(0, NOMAD)
        from autopilot import permissions
    return permissions


def generated_settings(state_dir):
    """settings_for(project, worktree_path, task_id) -> (settings path, [Wrapper]): the reviewed
    template plus the project's entry, and its command wrappers, written under `state_dir`.
    Raises projects.ConfigError when the entry would widen the template."""
    def settings_for(project, worktree, task_id):
        return _permissions().write(project, worktree, state_dir=state_dir, task_id=task_id)
    return settings_for


def cli_args(settings_path):
    return _permissions().cli_args(settings_path)


# ---- the prompt -------------------------------------------------------------------------------

def rules_file():
    """The shared rules: ~/nomad/docs/AUTONOMY.md once installed, else the reviewed draft."""
    for p in (RULES, RULES_DRAFT):
        if os.path.isfile(p):
            return p
    raise WorkerError(f"no shared rules file at {RULES} or {RULES_DRAFT}")


def _describe(cmd):
    extra = []
    if cmd.locks:
        extra.append(f"holds lock(s) {', '.join(cmd.locks)}")
    if cmd.env:
        extra.append("env " + " ".join(f"{k}={v}" for k, v in cmd.env.items()))
    return f"`{cmd.run}`" + (f" ({'; '.join(extra)})" if extra else "")


def commands_block(project, wrappers=None):
    """The project's commands for the prompt. With `wrappers` (permissions.Wrapper), each is
    named by its wrapper, the only form the permission profile allows."""
    ap = project.autopilot
    if not ap or not (ap.test or ap.pre_merge_gate or ap.build):
        return "The project declares no test commands. Per the rules, report *needs operator*."
    by_cmd = {}
    for w in wrappers or ():
        by_cmd.setdefault(repr(w.command), w)

    def call(c):
        w = by_cmd.get(repr(c))
        if w is None:
            return _describe(c)
        return f"`{w.path}" + (" [filter]`" if w.takes_filter else "`") + f" (runs {_describe(c)})"

    lines = []
    if wrappers:
        lines.append("Run each one ONLY through its wrapper script, exactly as shown, from your "
                     "worktree. The wrapper takes the locks, sets the environment and checks the "
                     "filter; the raw command is refused by your permissions. `[filter]` is one "
                     "optional test filter argument.")
    for name, c in ap.test.items():
        lines.append(f"- test `{name}`: {call(c)}" + ("" if wrappers else "  ({filter} = optional test filter)"))
    if ap.pre_merge_gate:
        lines.append("- pre-merge gate, in order: " + ", ".join(call(c) for c in ap.pre_merge_gate))
    if ap.build:
        lines.append(f"- build: {call(ap.build)}")
    for c in ap.post_test:
        lines.append(f"- after every test run: {call(c)}")
    if ap.known_failures:
        lines.append(f"- known failures: {', '.join(sorted(ap.known_failures))}")
    if ap.flake_set:
        lines.append(f"- flake set: {', '.join(sorted(ap.flake_set))}")
    return "\n".join(lines)


def build_prompt(project, task, wt, deadline, wrappers=None):
    tid = task["task"]
    title = task.get("title") or task.get("subject") or ""
    parts = [
        f"You are a NOMAD autopilot worker for project `{project.id}`. Your task is nomad-pm "
        f"#{tid}" + (f" \"{title}\"" if title else "") + ".",
        f"Read the shared rules at {rules_file()} and follow them exactly.",
    ]
    local = os.path.join(project.repo, "AUTONOMY.md")
    if os.path.isfile(local):
        parts.append(f"Then read the project's own rules at {local}; they add restrictions, "
                     "and where they disagree with the shared rules the stricter one wins.")
    parts.append(f"Your worktree is {wt.path} on branch `{wt.branch}` (from `{wt.base}`). "
                 f"The main checkout {project.repo} is read-only for you.")
    if task.get("promote") or task.get("status") == "New":
        parts.append("The task is New. Before you claim it, promote it to Ready with the comment "
                     "`autopilot: promoted` (rules section 1, step 2), after checking the "
                     "conditions listed there.")
    if task.get("criteria_draft"):
        parts.append("The task's acceptance criteria are in its `autopilot: criteria draft` "
                     "comment (drafted by the autopilot from the description, a delegated "
                     "decision). They count as its acceptance criteria: build to them.")
    decision, answer = task.get("decision"), (task.get("answer") or "").strip()
    if decision:
        verb = "approved it" if decision == "approved" else "requested changes"
        parts.append("The operator answered the question you left when you moved this task to "
                     f"Awaiting approval: they {verb}. The task is now {task.get('status')}. "
                     "Claim it (move it to In Progress) and continue on the existing branch.")
        if answer:
            parts.append("Their answer, verbatim:\n\n" + answer + "\n\nFollow it. Where it "
                         "disagrees with your earlier plan, the answer wins.")
        elif decision == "approved":
            parts.append("They left no text: go ahead as you proposed.")
        else:
            parts.append("They left no text. Read the task's latest comments for the reason; if "
                         "it is still unclear, ask again and report `needs operator`.")
    elif not wt.new_branch:
        parts.append("The branch already existed: an earlier run worked on this task. Read the "
                     "task's latest `autopilot: resume` comment and continue from there.")
    if wt.dirty:
        parts.append("The worktree holds uncommitted changes from an earlier run. They are that "
                     "run's work: review them, keep what belongs to the task, and commit.")
    parts.append("Declared commands:\n" + commands_block(project, wrappers))
    parts.append(f"Your time limit ends at {time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline))} "
                 "local time. Leave yourself 15 minutes: commit, comment `autopilot: resume` with "
                 "what is done and left, and stop. A run still going at the limit is killed.")
    parts.append("End with the tracker comment `autopilot: result <outcome>` the rules describe, "
                 "and repeat that same `autopilot: result <outcome>` line as the last line of your "
                 "final message.")
    return "\n\n".join(parts)


# ---- reading a run's stream-json --------------------------------------------------------------

def _events(path):
    try:
        f = open(path, errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def find_marker(text):
    """The outcome of the last `autopilot: result <outcome>` in text, mapped, or None."""
    found = MARKER.findall(text or "")
    return OUTCOMES[found[-1].lower()] if found else None


def parse_stream(path):
    """Everything collect needs from one run's log.

    Returns {"result": text or None, "cost_usd", "denials": [{"tool", "command"|"input"}],
    "is_error", "subtype", "marker", "shell_calls": [{"id", "command", "refused"}]}."""
    out = {"result": None, "cost_usd": 0.0, "denials": [], "is_error": False, "subtype": None,
           "marker": None, "shell_calls": [], "last_text": ""}
    calls, results = [], {}
    for ev in _events(path):
        t = ev.get("type")
        if t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use" and c.get("name") in SHELL_TOOLS:
                    calls.append({"id": c.get("id"), "command": (c.get("input") or {}).get("command", "")})
                elif c.get("type") == "text" and c.get("text"):
                    out["last_text"] = c["text"]
        elif t == "user":
            content = (ev.get("message") or {}).get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        results[c.get("tool_use_id")] = c
        elif t == "result":
            out["result"] = ev.get("result")
            out["cost_usd"] = float(ev.get("total_cost_usd") or 0)
            out["is_error"] = bool(ev.get("is_error"))
            out["subtype"] = ev.get("subtype")
            out["denials"] = [
                {"tool": d.get("tool_name"), "id": d.get("tool_use_id"),
                 **({"command": d["tool_input"]["command"]}
                    if isinstance(d.get("tool_input"), dict) and "command" in d["tool_input"]
                    else {"input": d.get("tool_input")})}
                for d in ev.get("permission_denials") or []]
    denied_ids = {d["id"] for d in out["denials"]}
    for c in calls:
        r = results.get(c["id"]) or {}
        c["refused"] = c["id"] in denied_ids or (
            bool(r.get("is_error")) and "denied" in _text(r.get("content")).lower()
            and "permission" in _text(r.get("content")).lower())
    out["shell_calls"] = calls
    out["marker"] = find_marker(out["result"] if out["result"] is not None else out["last_text"])
    return out


def judge(parsed, tracker_marker=None, returncode=None, stderr_tail="", tracker_checked=False):
    """(outcome, detail) for a run that ended by itself."""
    calls = parsed["shell_calls"]
    if calls and calls[0]["refused"] and not any(not c["refused"] for c in calls[1:]):
        return "error", (f"first shell command refused, and no shell command got through: "
                         f"{calls[0]['command']!r}. Check the run's permission profile.")
    if parsed["result"] is None:
        return "failed", (f"no result event (exit {returncode})"
                          + (f": {stderr_tail}" if stderr_tail else ""))
    text, marker = parsed["marker"], tracker_marker
    notes = []
    if parsed["is_error"]:
        notes.append(f"run ended with error ({parsed['subtype']})")
    if marker and text and marker != text:
        notes.append(f"final message says {text!r}, tracker says {marker!r}; using the tracker")
    outcome = marker or text
    if not outcome:
        return "failed", "; ".join(["no `autopilot: result` marker in the final message"
                                    + (" or the task's nomad-pm comments" if tracker_checked else "")] + notes)
    first = calls[0] if calls else None
    if first and first["refused"]:
        notes.append(f"first shell command was refused: {first['command']!r}")
    return outcome, "; ".join(notes)


def tracker_reader(project=None):
    """A callable task_id -> latest `autopilot: result` outcome from its nomad-pm comments, or
    None. Uses v2/pm_openproject.py (one GET of the activities); any failure reads as None, so
    the final message decides. Returns None itself if the client cannot be loaded."""
    try:
        f = os.path.join(NOMAD, "v2", "pm_openproject.py")
        spec = importlib.util.spec_from_file_location("nomad_pm_openproject", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        client = mod.OpenProject(project=project)
    except Exception as e:
        log.info("tracker cross-check off: %s", e)
        return None

    def read(task_id):
        try:
            texts = [(a.get("comment") or {}).get("raw") or "" for a in client.activities(task_id)]
        except Exception as e:
            log.info("tracker read for #%s failed: %s", task_id, e)
            return None
        for t in reversed(texts):
            m = find_marker(t)
            if m:
                return m
        return None
    return read


# ---- processes a restarted supervisor finds again ----------------------------------------------

def proc_start(pid):
    """The process's start time in clock ticks since boot (/proc/<pid>/stat field 22), or None
    when there is no such live process (gone, or a zombie). pid plus start time names one
    process for good: a recycled pid has another start time."""
    if not pid:
        return None
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    rest = data.rsplit(")", 1)[-1].split()
    if len(rest) < 20 or rest[0] in ("Z", "X", "x"):
        return None
    return int(rest[19])


class Adopted:
    """The Popen surface (pid, poll, wait, returncode) for a run started by an earlier
    supervisor. It is not our child, so its exit code is unknown (-1); its log says the rest."""

    def __init__(self, pid, start):
        self.pid = pid
        self.start = start
        self.returncode = None

    def poll(self):
        if self.returncode is None and (self.start is None or proc_start(self.pid) != self.start):
            self.returncode = -1
        return self.returncode

    def wait(self, timeout=None):
        end = None if timeout is None else time.time() + timeout
        while self.poll() is None:
            if end is not None and time.time() >= end:
                raise subprocess.TimeoutExpired("adopted run", timeout)
            time.sleep(0.05)
        return self.returncode


def record_path(log_path):
    return (log_path[:-len(".jsonl")] if log_path.endswith(".jsonl") else log_path) + ".run.json"


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---- the pool ---------------------------------------------------------------------------------

@dataclass
class Run:
    project: str
    task: str
    worktree: str
    branch: str
    log: str
    stderr: str
    settings: str
    proc: subprocess.Popen         # or Adopted
    started: float
    deadline: float
    killed: str = ""               # "", "timeout", "operator" or "lost" (died with the supervisor)
    reported: dict = field(default_factory=dict)
    pid_start: int = None          # proc_start(pid) at launch
    record: str = ""               # the run record file

    def public(self):
        return {"task": self.task, "branch": self.branch, "worktree": self.worktree,
                "log": self.log, "pid": self.proc.pid, "pid_start": self.pid_start,
                "started": self.started, "deadline": self.deadline}

    def record_data(self):
        return {"project": self.project, **self.public(), "stderr": self.stderr,
                "settings": self.settings}


def _kill_group(proc, grace=KILL_GRACE_SECS):
    """SIGTERM the run's process group, SIGKILL it after `grace` seconds."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)


class WorkerPool:
    """Live runs for every project. Not thread-safe by itself; the supervisor serialises passes.

    project_of(pid) -> projects.Project (default: the hub registry, projects.hub_get);
    settings_for(project, worktree_path, task_id) -> (settings path, [permissions.Wrapper])
    (default: generated_settings, the #875 profile); tracker(task_id) -> outcome or None."""

    def __init__(self, project_of=None, settings_for=None, tracker=None, claude=None,
                 run_secs=None, state_dir=None):
        self.project_of = project_of or projects_mod.hub_get
        self.state_dir = state_dir or STATE_DIR
        self.settings_for = settings_for or generated_settings(self.state_dir)
        self.tracker = tracker
        self.claude = claude or CLAUDE
        self.run_secs = RUN_SECS if run_secs is None else run_secs
        self.runs = {}          # (project, task) -> Run
        self.start_errors = {}  # project -> the last start_workers' failures

    def live(self, pid=None):
        return [r for r in self.runs.values()
                if (pid is None or r.project == pid) and r.proc.poll() is None]

    # -- start --

    def launch(self, project, task, settings_path=None, prompt=None):
        """Settings, worktree, prompt, `claude -p`. Returns the Run; does not wait for it.
        `settings_path` skips the settings builder; `prompt` replaces the worker prompt (smoke
        tests only). The settings come first, so a projects.yaml entry the generator refuses
        (projects.ConfigError, raised through) leaves no worktree behind. When ensure_worktree
        adopts a lane that already holds the task branch, the settings are rendered again for that
        lane; a lane that cannot get a profile (outside worktree_root) is a WorkerError."""
        tid = str(task["task"])
        key = (project.id, tid)
        if key in self.runs and self.runs[key].proc.poll() is None:
            raise WorkerError(f"task {tid} already has a live worker")
        root = worktree_root(project)
        wrappers = []
        settings = settings_path
        planned = os.path.join(root, f"ap-{tid}")
        if settings is None:
            settings, wrappers = self.settings_for(project, planned, tid)
        wt = ensure_worktree(project.repo, root, tid, task.get("title") or task.get("subject") or "",
                             regenerated=project.autopilot.regenerated if project.autopilot else ())
        if settings_path is None and os.path.realpath(wt.path) != os.path.realpath(planned):
            # An adopted lane (#944): rules rendered for ap-<id> would refuse every edit and every
            # `git -C <worktree>` write in the folder the run actually starts in. Adoption creates
            # nothing, so a refusal here still leaves no worktree behind.
            try:
                settings, wrappers = self.settings_for(project, wt.path, tid)
            except (projects_mod.ConfigError, _permissions().ConfigError) as e:
                raise WorkerError(f"{wt.branch} is checked out in {wt.path}, which cannot get a "
                                  f"worker profile: {e}") from e

        runs_dir = os.path.join(self.state_dir, "runs", project.id)
        os.makedirs(runs_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(runs_dir, f"{tid}-{stamp}.jsonl")
        err_path = os.path.join(runs_dir, f"{tid}-{stamp}.stderr")

        now = time.time()
        deadline = now + self.run_secs
        cmd = [self.claude, "-p", prompt or build_prompt(project, task, wt, deadline, wrappers),
               *cli_args(settings), "--verbose", "--output-format", "stream-json"]
        with open(log_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(cmd, cwd=wt.path, stdout=out, stderr=err,
                                    stdin=subprocess.DEVNULL, start_new_session=True,
                                    env={**os.environ, "ANTHROPIC_API_KEY": ""})
        run = Run(project.id, tid, wt.path, wt.branch, log_path, err_path, settings, proc,
                  now, deadline, pid_start=proc_start(proc.pid), record=record_path(log_path))
        run.reported = {"dirty": wt.dirty, "created": wt.created, "new_branch": wt.new_branch}
        _write_json(run.record, run.record_data())
        self.runs[key] = run
        return run

    def start_workers(self, ctx, tasks):
        """Scheduler hook. One run per task; a task that cannot start is logged and skipped."""
        project = self.project_of(ctx.project_id)
        if project is None:
            raise WorkerError(f"project {ctx.project_id!r} not in projects.yaml")
        started = []
        errors = self.start_errors[ctx.project_id] = []
        for t in tasks:
            try:
                run = self.launch(project, t)
            except (WorkerError, OSError) as e:
                errors.append(f"task {t.get('task')}: {e}")
                ctx.event("error", f"task {t.get('task')}: worker not started: {e}")
                continue
            r = run.reported
            if r["dirty"]:
                ctx.event("worktree", f"task {run.task}: {run.worktree} had uncommitted changes; "
                                      "kept as is and handed to the run")
            elif not r["new_branch"]:
                ctx.event("worktree", f"task {run.task}: resuming on {run.branch}")
            started.append(run.public())
        return started

    # -- collect --

    def _finish(self, run):
        parsed = parse_stream(run.log)
        denials = parsed["denials"]
        requeue = False
        if run.killed == "lost" and parsed["result"] is not None:
            run.killed = ""          # it ended by itself while the supervisor was down: judge it
        if run.killed:
            why = {"timeout": "time limit reached", "operator": "killed by the operator",
                   "lost": "the run ended without a result while the supervisor was down"}[run.killed]
            outcome, detail = "resume", f"{why}; branch {run.branch} kept"
            requeue = True
        else:
            tail = ""
            try:
                with open(run.stderr, errors="replace") as f:
                    tail = f.read()[-500:].strip()
            except OSError:
                pass
            tracker = None
            if self.tracker and parsed["result"] is not None:
                tracker = self.tracker(run.task)
            outcome, detail = judge(parsed, tracker, run.proc.returncode, tail,
                                    tracker_checked=bool(self.tracker))
        out = {"task": run.task, "outcome": outcome, "cost_usd": parsed["cost_usd"],
               "detail": detail, "denials": denials, "result": parsed["result"],
               "branch": run.branch, "worktree": run.worktree, "log": run.log,
               "elapsed_s": round(time.time() - run.started, 1),
               # the supervisor, not the run, decided `resume`: nothing told the tracker yet
               "requeue": requeue}
        if run.record:
            try:
                _write_json(run.record, {**run.record_data(), "collected": outcome,
                                         "collected_at": time.time()})
            except OSError as e:
                log.warning("run record %s not updated: %s", run.record, e)
        return out

    def collect(self, ctx, now=None):
        """Scheduler hook. Kill runs past their deadline, then report every run that ended."""
        now = time.time() if now is None else now
        done = []
        for key, run in list(self.runs.items()):
            if run.project != ctx.project_id:
                continue
            if run.proc.poll() is None and now >= run.deadline:
                run.killed = "timeout"
                _kill_group(run.proc)
            if run.proc.poll() is None:
                continue
            done.append(self._finish(run))
            del self.runs[key]
        return done

    # -- recovery --

    def _records(self, pid):
        """Uncollected run records on disk for `pid`, newest per task."""
        out = {}
        for f in sorted(glob.glob(os.path.join(self.state_dir, "runs", pid, "*.run.json"))):
            try:
                with open(f) as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            if rec.get("collected") or rec.get("task") is None:
                continue
            t = str(rec["task"])
            if t not in out or rec.get("started", 0) >= out[t].get("started", 0):
                out[t] = {**rec, "record": f}
        return out

    def recover(self, ctx, stored=()):
        """A restarted supervisor finds its runs again. Candidates are the stored workers plus
        every uncollected run record on disk (a run started just before a crash may never have
        reached the state file). A run whose process (pid and start time) still lives is
        adopted and collected when it ends. A dead one is collected on the next pass: judged by
        its log when the log has a final result, else `resume`. Worktrees `ap-<id>` with no run
        are reported and left alone. Returns the worker list to store."""
        pid = ctx.project_id
        cands = self._records(pid)
        for w in stored:
            t = str(w.get("task"))
            if t not in cands:
                cands[t] = {**w, "record": record_path(w["log"]) if w.get("log") else ""}
        workers = []
        for t, rec in sorted(cands.items()):
            key = (pid, t)
            if key in self.runs:
                workers.append(self.runs[key].public())
                continue
            if not rec.get("log"):
                ctx.event("recover", f"task {t}: stored worker has no run log; dropped")
                continue
            proc = Adopted(rec.get("pid"), rec.get("pid_start"))
            started = rec.get("started") or time.time()
            run = Run(pid, t, rec.get("worktree", ""), rec.get("branch", ""), rec["log"],
                      rec.get("stderr") or rec["log"][:-len(".jsonl")] + ".stderr",
                      rec.get("settings", ""), proc, started,
                      rec.get("deadline") or started + self.run_secs,
                      pid_start=rec.get("pid_start"), record=rec.get("record", ""))
            alive = proc.poll() is None
            if not alive:
                run.killed = "lost"
            self.runs[key] = run
            workers.append(run.public())
            ctx.event("recover", f"task {t}: " + (
                f"run still going (pid {proc.pid}), adopted" if alive else
                "run ended while the supervisor was down; collected on the next pass"))
        project = None
        try:
            project = self.project_of(pid)
        except Exception as e:
            ctx.event("recover", f"worktrees not checked: {type(e).__name__}: {e}")
        if project is not None:
            for d in sorted(glob.glob(os.path.join(worktree_root(project), "ap-*"))):
                t = os.path.basename(d)[3:]
                if t in cands or not os.path.isdir(d):
                    continue
                branch = git(d, "branch", "--show-current") or "?"
                ctx.event("recover", f"worktree {d} on {branch} kept from an earlier run, no "
                                     f"live worker; task #{t} continues there when it is picked")
        return workers

    # -- stop --

    def stop(self, ctx, mode):
        """Scheduler hook. "finish": runs go on and are collected as they end. "kill": their
        process groups die now; branches and worktrees stay, and the next collect reports them
        as `resume`."""
        if mode != "kill":
            return
        for run in self.live(ctx.project_id):
            run.killed = "operator"
            _kill_group(run.proc)


def worktree_root(project):
    ap = project.autopilot
    return ap.worktree_root if ap and ap.worktree_root else project.repo + "-wt"


def scheduler_hooks(base, pool=None):
    """A subclass of the Scheduler class `base` whose worker hooks use a WorkerPool, e.g.
    `Supervisor(store, scheduler_hooks(supervisor.Scheduler)())`. Task selection (#865) and the
    merge queue (#871) stay with `base`."""
    shared = pool or WorkerPool()

    class WorkerScheduler(base):
        pool = shared

        def start_workers(self, ctx, tasks):
            return self.pool.start_workers(ctx, tasks)

        def collect(self, ctx):
            return self.pool.collect(ctx)

        def stop(self, ctx, mode):
            return self.pool.stop(ctx, mode)

        def recover(self, ctx):
            return self.pool.recover(ctx, ctx.project.get("workers", []))

    return WorkerScheduler
