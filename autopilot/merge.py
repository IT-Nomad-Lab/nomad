"""The autopilot's merge queue: one per project, one merge at a time (PRD 5.5, AC4, AC7, #871).

The supervisor's `Scheduler.merge(ctx, finished)` hook hands it what `collect` returned. Every
finished worker whose outcome is `merge` is queued (persisted, so a restart resumes it), and a
per-project background thread works the queue one branch at a time:

1. In the task's worktree, merge the project's current default branch into the task branch
   (`git -C <worktree> merge --no-edit <default>`).
   - Conflict: the queue aborts its own merge, comments `autopilot: resume` naming the files and
     asking the next worker run to merge the default branch and resolve them, and moves the task
     to In Progress, where task selection picks it up again. The project is not stalled. After
     CONFLICT_LIMIT conflicts in a row the task goes to Blocked instead.
2. Run the full pre-merge gate in the worktree, step by step, through the same generated
   wrappers the workers use (permissions.write_wrappers), so locks and env apply. `post_test`
   runs after every test step and every rerun, pass or fail. The entry is marked `gate_started`
   first: if a restart cuts the gate short, the next pass runs `post_test` and resets the
   `regenerated` paths before the dirty check (#985). Files whose whole change is the project's
   `noise` (Unity import noise, #996) are reset before the dirty check on every pass.
   - Results per test suite come from the project's `autopilot.results` format: `exit` (the
     exit code only) or `nunit` (NUnit 3 XML: counts and failing test names). Other steps
     (compile, build) are exit code only.
   - Pass rule: every failing test is a known failure, or a flake that then passes 3 solo reruns
     in a row; and no suite's test count is below the last green gate's (stored per project;
     the first green gate records the baseline).
   - A real failure: the first time, `autopilot: resume` with the failing tests (a worker fixes
     it); the second time in a row, Blocked with the failing tests named.
3. Fast-forward the default branch, never checking anything out (#1010): `merge --ff-only <sha>`
   where it is checked out (the main checkout, else any worktree that has it); when nobody has it
   checked out (the shared main checkout sits on someone's feature branch), a compare-and-swap
   `git update-ref refs/heads/<default> <sha> <old>`, only when <old> is an ancestor of <sha>.
   - Refused because of local changes (or any reason but "not a fast-forward"): the task goes
     to Awaiting approval (needs operator) with git's reason. Uncommitted files are checksummed
     before and after; nothing is stashed, reset or cleaned. Never pushes.
   - Not a fast-forward (the default branch moved meanwhile): once more from step 1.
4. Task -> Done with a comment: the commit, the test table (suite, total, passed, failed,
   skipped), known failures seen and flakes rerun.

Tracker writes go through `Tracker` (v2/pm_openproject.py, as tasks.py reads it); tests pass a
fake. A tracker failure is logged as a project event and never stops the queue.

    q = MergeQueue()
    q.merge(ctx, finished)        # queues, starts the project's thread, returns at once
    q.busy("runematch")           # task ids queued or merging
    q.result("runematch", "833")  # the last result for a task that left the queue (#890)

Run the tests: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest autopilot -q
"""
import glob
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if NOMAD not in sys.path:
    sys.path.insert(0, NOMAD)       # permissions.py imports `autopilot.projects` absolutely

from autopilot import permissions  # noqa: E402
from autopilot import projects as projects_mod  # noqa: E402
from autopilot.worker import (  # noqa: E402
    GrantLedger,
    config_changes,
    default_branch,
    reset_noise,
    reset_regenerated,
)

log = logging.getLogger("autopilot.merge")

STATE_DIR = os.path.expanduser(os.environ.get("NOMAD_AUTOPILOT_STATE_DIR",
                                              "~/.local/state/nomad-autopilot"))
GATE_SECS = float(os.environ.get("NOMAD_AUTOPILOT_GATE_SECS", str(3 * 3600)))  # per step
GIT_SECS = 300
SOLO_RERUNS = 3             # AUTONOMY: a flake passes only after 3 solo reruns in a row
FAILURE_LIMIT = 2           # PRD 5.5: two real failures in a row -> Blocked
CONFLICT_LIMIT = 3          # conflicts in a row before Blocked (PRD 8: lower the cap)
RESULTS_KEPT = 100          # per project: the last results, for run mode "once" (#890)
MTIME_SLACK = 2.0           # seconds: a results file this much older than the step still counts

# Statuses (nomad-pm standard flow) the queue leaves a task in.
DONE, RESUME, BLOCKED, OPERATOR = "Done", "In Progress", "Blocked", "Awaiting approval"


# ---- test results -----------------------------------------------------------------------------

class ResultsError(RuntimeError):
    """No results file, or one that cannot be read."""


@dataclass
class SuiteRun:
    """What one test-suite run reported."""
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0            # skipped, ignored, explicit and inconclusive
    failures: list = field(default_factory=list)       # full names of failed test cases
    passed_names: set = field(default_factory=set)
    result: str = ""            # the run's overall result attribute
    file: str = ""


def parse_nunit(path):
    """Counts and failing test names from an NUnit 3 results file (Unity's -testResults)."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as e:
        raise ResultsError(f"cannot read NUnit results {path}: {e}")
    if root.tag != "test-run":
        raise ResultsError(f"{path} is not an NUnit 3 test-run (root <{root.tag}>)")
    r = SuiteRun(result=root.get("result", ""), file=path)
    for case in root.iter("test-case"):
        name = case.get("fullname") or case.get("name") or "?"
        res = (case.get("result") or "").split("(")[0]
        r.total += 1
        if res == "Passed":
            r.passed += 1
            r.passed_names.add(name)
        elif res == "Failed":
            r.failed += 1
            r.failures.append(name)
        else:                       # Skipped, Inconclusive, Warning
            r.skipped += 1
    return r


_XML_IN_OUTPUT = re.compile(r"""([^\s'"()<>|;,]+\.xml)\b""")


def find_results(output, cwd, since, pattern=""):
    """The results file of a run that started at `since`: the last `.xml` path its output names
    (unity.sh prints `results: <path>`), else the newest match of `pattern` under cwd. A file
    older than the run never counts, so a stale result can't pass a gate."""
    def fresh(p):
        try:
            return os.path.isfile(p) and os.path.getmtime(p) >= since - MTIME_SLACK
        except OSError:
            return False
    for m in reversed(_XML_IN_OUTPUT.findall(output or "")):
        p = m if os.path.isabs(m) else os.path.join(cwd, m)
        if fresh(p):
            return p
    if pattern:
        cands = [p for p in glob.glob(os.path.join(cwd, pattern)) if fresh(p)]
        if cands:
            return max(cands, key=os.path.getmtime)
    return None


class ExitResults:
    """`results: exit`: the exit code is all there is. No names, so no flakes or known failures."""
    name = "exit"

    def read(self, code, output, cwd, since):
        return None


class NUnitResults:
    """`results: nunit`: counts and failing names from the XML the run wrote."""
    name = "nunit"

    def __init__(self, pattern=""):
        self.pattern = pattern

    def read(self, code, output, cwd, since):
        f = find_results(output, cwd, since, self.pattern)
        if not f:
            raise ResultsError("no results file written by this run"
                               + (f" (looked in the output and {self.pattern})" if self.pattern else ""))
        return parse_nunit(f)


def results_reader(ap):
    """The reader for a project's `autopilot.results` format. Add a format here and in
    projects.RESULT_FORMATS."""
    if ap.results == "nunit":
        return NUnitResults(ap.results_files)
    return ExitResults()


# ---- running commands ---------------------------------------------------------------------------

def run_command(argv, cwd, log_path, timeout):
    """Run argv in cwd in its own process group, output to log_path. Returns (code, output).
    A run past `timeout` seconds is killed (group) and reports 124, like timeout(1)."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "wb") as out:
        p = subprocess.Popen(argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    p.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            code = 124
    with open(log_path, errors="replace") as f:
        return code, f.read()


def git(path, *args, timeout=GIT_SECS, strip=True):
    """(returncode, stdout, stderr) of `git -C path args`. Never raises on a git error."""
    p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip() if strip else p.stdout, p.stderr.strip()


def _git_dir_file(worktree, name):
    rc, out, _ = git(worktree, "rev-parse", "--git-path", name)
    if rc != 0:
        return None
    return out if os.path.isabs(out) else os.path.join(worktree, out)


def merge_in_progress(worktree):
    f = _git_dir_file(worktree, "MERGE_HEAD")
    return bool(f and os.path.exists(f))


def checkouts_of(repo, branch):
    """Every worktree of `repo` (the main checkout included) that has `branch` checked out."""
    rc, out, _ = git(repo, "worktree", "list", "--porcelain")
    paths, cur = [], None
    for line in (out.splitlines() if rc == 0 else []):
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            paths.append(cur)
    return paths


def move_ref(repo, branch, sha):
    """Fast-forward `branch`, checked out nowhere, to `sha` (#1010). Same answers as
    MergeQueue.fast_forward. `update-ref <ref> <new> <old>` is a compare-and-swap: if the branch
    moved after we read it, git refuses and the queue's not_ff retry merges again."""
    ref = f"refs/heads/{branch}"
    rc, old, err = git(repo, "rev-parse", "--verify", "--quiet", ref)
    if rc != 0:
        return "refused", f"`{branch}` does not exist in {repo}"
    if old == sha:
        return "merged", ""
    if git(repo, "merge-base", "--is-ancestor", old, sha)[0] != 0:
        return "not_ff", f"`{branch}` is at {old[:10]}, which {sha[:10]} does not contain"
    rc, out, err = git(repo, "update-ref", "-m", f"autopilot: fast-forward to {sha[:10]}",
                       ref, sha, old)
    if rc != 0:
        return "not_ff", _tail(f"{out}\n{err}", 4)
    return "merged", ""


def dirty_checksums(repo):
    """sha256 of every modified or untracked (not ignored) file in a checkout: AC7's proof that
    the operator's local edits are untouched."""
    rc, out, _ = git(repo, "status", "--porcelain", "-z", "--untracked-files=all", strip=False)
    sums = {}
    if rc != 0:
        return sums
    parts = out.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1                  # -z puts the rename source in the next field
        full = os.path.join(repo, path)
        if os.path.isfile(full) and not os.path.islink(full):
            with open(full, "rb") as f:
                sums[path] = hashlib.sha256(f.read()).hexdigest()
        else:
            sums[path] = None       # deleted, or a folder: present in the list is what counts
    return sums


# ---- the gate -----------------------------------------------------------------------------------

@dataclass
class Step:
    name: str
    test: bool                  # a test suite (results parsed, post_test after it)
    ok: bool = False
    code: int = None
    run: SuiteRun = None
    known: list = field(default_factory=list)       # failures that are known failures
    flakes: list = field(default_factory=list)      # flakes that then passed 3 solo reruns
    real: list = field(default_factory=list)        # what failed for real, human-readable
    log: str = ""


@dataclass
class Gate:
    ok: bool
    steps: list

    @property
    def real(self):
        return [r for s in self.steps for r in s.real]

    @property
    def flakes(self):
        return [f for s in self.steps for f in s.flakes]

    @property
    def known(self):
        return [k for s in self.steps for k in s.known]

    def totals(self):
        return {s.name: s.run.total for s in self.steps if s.run is not None}


def _tail(text, n=15):
    lines = [x for x in (text or "").splitlines() if x.strip()]
    return "\n".join(lines[-n:])


def gate_argv(network, argv):
    """argv for one supervisor gate step under the project's `gate_network` (#1530). host: as is.
    none: in a network namespace of its own (bwrap --unshare-net), which cuts TCP and UDP,
    loopback included. Unix sockets (Docker's too), the filesystem and the environment are not
    cut. None when bubblewrap is missing: the caller fails the step and never runs argv."""
    if network == "host":
        return list(argv)
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return None
    return [bwrap, "--unshare-net", "--dev-bind", "/", "/", "--die-with-parent", "--", *argv]


class GateRunner:
    """Runs one project's pre-merge gate in one worktree through its wrappers. Every step,
    solo rerun and post-test goes through _run, so `gate_network` covers them all."""

    def __init__(self, project, wrappers, log_dir, runner=run_command, timeout=GATE_SECS):
        self.project = project
        self.ap = project.autopilot
        self.wrappers = wrappers
        self.log_dir = log_dir
        self.runner = runner
        self.timeout = timeout
        self.reader = results_reader(self.ap)
        self._n = 0

    def wrapper_for(self, cmd):
        for w in self.wrappers:
            if w.command == cmd:
                return w
        raise RuntimeError(f"no wrapper for gate command {cmd.run!r}")

    def suite_name(self, cmd):
        for k, c in self.ap.test.items():
            if c == cmd:
                return k
        return None

    def _run(self, wt, wrapper, label, *args):
        self._n += 1
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:80]
        path = os.path.join(self.log_dir, f"{self._n:02d}-{safe}.log")
        start = time.time()
        argv = gate_argv(self.ap.gate_network, [wrapper.path, *args])
        if argv is None:
            out = (f"gate_network is none but bubblewrap (bwrap) is not installed: `{label}` "
                   "was not run, so it can't run with the network on\n")
            os.makedirs(self.log_dir, exist_ok=True)
            with open(path, "w") as f:
                f.write(out)
            return 126, out, start, path
        code, out = self.runner(argv, wt, path, self.timeout)
        return code, out, start, path

    def post_test(self, wt):
        notes = []
        for i, c in enumerate(self.ap.post_test):
            code, out, _, _ = self._run(wt, self.wrapper_for(c), f"post-test-{i + 1}")
            if code != 0:
                notes.append(f"post_test `{c.run}` exit {code}: {_tail(out, 3)}")
        for n in notes:
            log.warning("%s: %s", self.project.id, n)
        return notes

    def run(self, wt, baseline=None):
        """Every gate step in order; stops at the first step with a real failure."""
        baseline = baseline or {}
        steps = []
        if self.ap.gate_network == "none":
            log.info("%s: gate network none: TCP and UDP cut, loopback included; Unix sockets "
                     "(Docker's too), files and the environment are not", self.project.id)
        for i, cmd in enumerate(self.ap.pre_merge_gate):
            suite = self.suite_name(cmd)
            step = Step(name=suite or cmd.render(), test=suite is not None)
            steps.append(step)
            w = self.wrapper_for(cmd)
            code, out, start, path = self._run(wt, w, step.name)
            step.code, step.log = code, path
            if step.test:
                self.post_test(wt)
                self._judge_suite(wt, step, w, code, out, start, baseline.get(step.name))
            else:
                step.ok = code == 0
                if not step.ok:
                    step.real.append(f"`{step.name}` exited {code}: {_tail(out, 5)}")
            if not step.ok:
                return Gate(False, steps)
        return Gate(True, steps)

    def _judge_suite(self, wt, step, wrapper, code, out, start, base):
        try:
            tr = self.reader.read(code, out, wt, start)
        except ResultsError as e:
            step.real.append(f"{step.name}: {e} (exit {code}): {_tail(out, 5)}")
            return
        step.run = tr
        if tr is None:                                  # exit-code-only suite
            step.ok = code == 0
            if not step.ok:
                step.real.append(f"{step.name}: exit {code}: {_tail(out, 5)}")
            return
        for name in tr.failures:
            if self.ap.is_known_failure(name):
                step.known.append(name)
            elif self.ap.is_flake(name):
                ok, why = self.solo_reruns(wt, wrapper, name)
                if ok:
                    step.flakes.append(name)
                else:
                    step.real.append(f"{name} (flake: {why})")
            else:
                step.real.append(name)
        if not tr.failures and (code != 0 or tr.result.startswith("Failed")):
            step.real.append(f"{step.name}: run failed (exit {code}, result {tr.result or '?'}) "
                             f"with no failing test case: {_tail(out, 5)}")
        if base is not None and tr.total < base:
            step.real.append(f"{step.name}: test count dropped {base} -> {tr.total}")
        step.ok = not step.real

    def solo_reruns(self, wt, wrapper, test):
        """(passed, why). The flake must pass SOLO_RERUNS solo runs in a row."""
        if not wrapper.takes_filter:
            return False, "the suite command takes no {filter}, so it cannot be rerun solo"
        if not permissions.validate_filter(test):
            return False, "its name is not a filter the wrapper accepts"
        for i in range(1, SOLO_RERUNS + 1):
            code, out, start, _ = self._run(wt, wrapper, f"rerun-{i}-{test}", test)
            self.post_test(wt)
            try:
                tr = self.reader.read(code, out, wt, start)
            except ResultsError as e:
                return False, f"solo rerun {i}: {e}"
            if tr is None or test not in tr.passed_names or tr.failures:
                return False, f"failed solo rerun {i}/{SOLO_RERUNS}"
        return True, ""


def format_table(gate):
    rows = ["| suite | total | passed | failed | skipped | result |", "|---|---|---|---|---|---|"]
    for s in gate.steps:
        verdict = "pass" if s.ok else "FAIL"
        if s.run is not None:
            r = s.run
            rows.append(f"| {s.name} | {r.total} | {r.passed} | {r.failed} | {r.skipped} | {verdict} |")
        else:
            rows.append(f"| {s.name} | - | - | - | - | {verdict} (exit {s.code}) |")
    return "\n".join(rows)


# ---- persisted queue state ----------------------------------------------------------------------

class MergeState:
    """merge-state.json next to the supervisor's state: the queue, each project's test-count
    baseline (last green gate) and each task's failure and conflict streaks. Atomic writes."""

    def __init__(self, path=None):
        self.path = path or os.path.join(STATE_DIR, "merge-state.json")
        self.lock = threading.RLock()
        self.data = {"projects": {}}
        try:
            with open(self.path) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("projects"), dict):
                self.data = d
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            bad = f"{self.path}.corrupt-{int(time.time())}"
            log.error("merge state unreadable (%s); moved to %s", e, bad)
            try:
                os.replace(self.path, bad)
            except OSError:
                pass

    def project(self, pid):
        with self.lock:
            p = self.data["projects"].setdefault(pid, {})
            p.setdefault("queue", [])
            p.setdefault("baseline", {})
            p.setdefault("tasks", {})
            p.setdefault("results", {})
            return p

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)


# ---- the tracker --------------------------------------------------------------------------------

class Tracker:
    """nomad-pm writes, through the engine's OpenProject client (v2/pm_openproject.py), loaded by
    path as tasks.py does. `move` walks legal hops and puts the comment on at the end."""

    def __init__(self, op_module=None):
        self._op = op_module
        self._client = None

    def _c(self):
        if self._client is None:
            if self._op is None:
                f = os.path.join(NOMAD, "v2", "pm_openproject.py")
                spec = importlib.util.spec_from_file_location("nomad_pm_openproject", f)
                self._op = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(self._op)
            self._client = self._op.OpenProject()
        return self._client

    def move(self, task, status, comment):
        self._c().move(int(task), status, comment)

    def comment(self, task, text):
        self._c().comment(int(task), text)


# ---- the queue ----------------------------------------------------------------------------------

class MergeQueue:
    """Serial merge queue per project. Plugs into supervisor.Scheduler as its `merge` hook.

    project_of(pid) -> projects.Project (projects.get by default); tracker: a Tracker or fake;
    runner(argv, cwd, log_path, timeout) -> (code, output) runs a gate command; background=False
    works the queue inside merge() and returns the results (tests); after_merge(ctx, result) is
    called after each merged task (the milestone gate, #872, hooks in there)."""

    def __init__(self, project_of=None, tracker=None, state=None, state_dir=None, runner=None,
                 background=True, gate_secs=None, after_merge=None):
        self.project_of = project_of or projects_mod.get
        self.tracker = tracker if tracker is not None else Tracker()
        self.state_dir = state_dir or STATE_DIR
        self.state = state or MergeState(os.path.join(self.state_dir, "merge-state.json"))
        self.runner = runner or run_command
        self.background = background
        self.gate_secs = GATE_SECS if gate_secs is None else gate_secs
        self.after_merge = after_merge
        self._locks = {}
        self._threads = {}
        self._guard = threading.Lock()

    # -- the hook --

    def merge(self, ctx, finished):
        """Queue every finished worker with outcome `merge`, then work the project's queue: in a
        background thread (default; returns []) or right here (background=False; returns the
        results). Call it with finished=[] to resume a queue left by a restart."""
        pid = ctx.project_id
        with self.state.lock:
            q = self.state.project(pid)["queue"]
            queued = {str(e["task"]) for e in q}
            added = False
            for f in finished or []:
                if f.get("outcome") != "merge" or str(f.get("task")) in queued:
                    continue
                q.append({"task": str(f["task"]), "branch": f.get("branch", ""),
                          "worktree": f.get("worktree", ""), "queued": time.time()})
                queued.add(str(f["task"]))
                added = True
                ctx.event("merge", f"task {f['task']}: queued for merge ({f.get('branch', '?')})")
            if added:
                self.state.save()
            if not q:
                return []
        if not self.background:
            return self.drain(ctx)
        with self._guard:
            t = self._threads.get(pid)
            if t is None or not t.is_alive():
                t = threading.Thread(target=self._drain_quietly, args=(ctx,),
                                     name=f"merge-{pid}", daemon=True)
                self._threads[pid] = t
                t.start()
        return []

    def busy(self, pid):
        """Task ids queued or merging for pid: task selection should leave them alone."""
        with self.state.lock:
            return [e["task"] for e in self.state.project(pid)["queue"]]

    def result(self, pid, tid):
        """The result ("merged", "resume", "blocked", "needs_operator") of the task's last trip
        through the queue, or None. Persisted with the queue, so it survives a restart."""
        with self.state.lock:
            r = self.state.project(pid)["results"].get(str(tid))
            return r.get("result") if r else None

    def running(self, pid):
        t = self._threads.get(pid)
        return bool(t and t.is_alive())

    def wait(self, pid, timeout=None):
        t = self._threads.get(pid)
        if t:
            t.join(timeout)

    # -- working the queue --

    def _lock(self, pid):
        with self._guard:
            return self._locks.setdefault(pid, threading.Lock())

    def _drain_quietly(self, ctx):
        try:
            self.drain(ctx)
        except Exception as e:
            log.exception("merge queue for %s died", ctx.project_id)
            ctx.event("error", f"merge queue stopped: {type(e).__name__}: {e}")

    def drain(self, ctx):
        """Merge queued branches one at a time until the queue is empty. One per project at once."""
        pid = ctx.project_id
        lock = self._lock(pid)
        if not lock.acquire(blocking=False):
            return []               # another thread is working this project's queue
        results = []
        try:
            while True:
                with self.state.lock:
                    q = self.state.project(pid)["queue"]
                    if not q:
                        return results
                    entry = dict(q[0])
                try:
                    res = self.process(ctx, entry)
                except Exception as e:
                    log.exception("merge of task %s failed", entry["task"])
                    res = self._finish(ctx, entry, "needs_operator", OPERATOR,
                                       f"autopilot: needs operator: the merge queue hit an error "
                                       f"on `{entry.get('branch')}`: {type(e).__name__}: {e}. "
                                       "Nothing was pushed; the default branch was not moved.")
                with self.state.lock:
                    p = self.state.project(pid)
                    q = p["queue"]
                    q[:] = [e for e in q if e["task"] != entry["task"]]
                    # Recorded in the same write that takes the task off the queue, so a reader
                    # never sees it gone from the queue with no result (#890).
                    rs = p["results"]
                    rs[str(entry["task"])] = {"result": res.get("result"), "t": time.time()}
                    for old in sorted(rs, key=lambda k: rs[k].get("t", 0))[:-RESULTS_KEPT]:
                        del rs[old]
                    self.state.save()
                results.append(res)
        finally:
            lock.release()

    # -- one branch --

    def process(self, ctx, entry):
        pid, tid = ctx.project_id, entry["task"]
        branch, wt = entry.get("branch", ""), entry.get("worktree", "")
        project = self.project_of(pid)
        if project is None or project.autopilot is None:
            return self._finish(ctx, entry, "needs_operator", OPERATOR,
                                f"autopilot: needs operator: project {pid!r} has no autopilot "
                                "block in projects.yaml, so there is no gate to merge with.")
        repo = project.repo
        default = default_branch(repo)
        if not project.autopilot.pre_merge_gate:
            return self._finish(ctx, entry, "needs_operator", OPERATOR,
                                f"autopilot: needs operator: project {pid!r} declares no pre-merge "
                                "gate (no `test` and no `pre_merge_gate`), so nothing can prove "
                                f"`{branch}` safe to merge. Not merged.")

        if not wt or not os.path.isdir(wt) or git(wt, "branch", "--show-current")[1] != branch:
            return self._resume(ctx, entry, f"the worktree `{wt}` is missing or not on `{branch}`. "
                                            "Recreate it from the branch and report ready to merge again.")
        if merge_in_progress(wt):           # left by an interrupted queue run: ours to abort
            git(wt, "merge", "--abort")
        ws = permissions.write_wrappers(project, self.state_dir)
        log_dir = os.path.join(self.state_dir, "merge", pid, f"{tid}-{time.strftime('%Y%m%d-%H%M%S')}")
        if entry.get("gate_started"):
            self._clean_interrupted_gate(ctx, entry, project, ws, log_dir)
        noise = reset_noise(wt, project.autopilot.noise)
        if noise:
            ctx.event("merge", f"task {tid}: reset noise {', '.join(noise)}")
        if git(wt, "status", "--porcelain", "--untracked-files=no")[1]:
            return self._resume(ctx, entry, "the worktree has uncommitted changes to tracked files. "
                                            "Commit or restore them, then report ready to merge.")

        pstate = self.state.project(pid)

        # #979, PRD 5.6: a branch that changed agent config without the operator's grant never
        # merges, whatever the worker reported. Every edit grant the task used in any run counts.
        granted = GrantLedger(os.path.join(self.state_dir, "grants.json")).granted_edits(pid, tid)
        config = config_changes(project, wt, granted, self.state_dir, base=default)
        if config:
            listed = "\n".join(f"- `{p}`" for p in config)
            return self._finish(ctx, entry, "needs_operator", OPERATOR,
                                f"autopilot: needs operator: `{branch}` changed agent config "
                                f"that no grant named:\n{listed}\n\nNot merged. To accept it, "
                                "comment `grant: edit <path>` for each path and move the task "
                                "to Ready; otherwise ask for it to be reverted."
                                + permissions.deny_glob_note(project, config))

        for attempt in (1, 2):
            rc, out, err = git(wt, "merge", "--no-edit", default)
            if rc != 0:
                _, files, _ = git(wt, "diff", "--name-only", "--diff-filter=U")
                if merge_in_progress(wt):
                    git(wt, "merge", "--abort")
                if files:
                    return self._conflict(ctx, entry, default, files.splitlines(), project)
                return self._resume(ctx, entry, f"merging `{default}` into `{branch}` failed: "
                                                f"{_tail(err or out, 5)}")
            ctx.event("merge", f"task {tid}: gate running on {branch} (attempt {attempt})")
            self._mark_gate_started(pid, tid)
            gate =GateRunner(project, ws, log_dir, self.runner, self.gate_secs).run(
                wt, dict(pstate["baseline"]))
            if not gate.ok:
                return self._gate_failed(ctx, entry, gate, log_dir)
            _, sha, _ = git(wt, "rev-parse", "HEAD")
            status, why = self.fast_forward(repo, default, sha)
            if status == "merged":
                return self._merged(ctx, entry, project, default, sha, gate, log_dir)
            if status == "not_ff" and attempt == 1:
                ctx.event("merge", f"task {tid}: {default} moved during the gate; merging again")
                continue
            if status == "not_ff":
                return self._resume(ctx, entry, f"`{default}` kept moving while the gate ran: {why}")
            return self._finish(ctx, entry, "needs_operator", OPERATOR,
                                f"autopilot: needs operator: the gate passed but the fast-forward "
                                f"of `{default}` in {repo} was refused: {why}\n\n"
                                f"Nothing was stashed, reset or checked out, and nothing was pushed. "
                                f"Commit or move the local changes where `{default}` is checked out, then move "
                                f"this task back to Ready: a worker resumes on `{branch}` "
                                f"({sha[:10]}, gate green) and reports it ready to merge again."
                                "\n\n" + format_table(gate),
                                gate=gate)

    def _mark_gate_started(self, pid, tid):
        """Persist that the gate is running on this entry, so a supervisor stopped mid-gate (#985)
        knows on restart that post_test never ran."""
        with self.state.lock:
            for e in self.state.project(pid)["queue"]:
                if e["task"] == tid:
                    e["gate_started"] = time.time()
            self.state.save()

    def _clean_interrupted_gate(self, ctx, entry, project, ws, log_dir):
        """A restart cut the last gate short (#985): its test process may have outlived the
        supervisor and rewritten files after post_test should have run. Run post_test and reset
        the `regenerated` paths now, before the dirty check, so tool output isn't mistaken for
        uncommitted work. Anything else stays dirty and is reported as before."""
        wt = entry["worktree"]
        GateRunner(project, ws, log_dir, self.runner, self.gate_secs).post_test(wt)
        reset = reset_regenerated(wt, project.autopilot.regenerated)
        ctx.event("merge", f"task {entry['task']}: cleaned up after an interrupted gate"
                           + (f" (reset {', '.join(reset)})" if reset else ""))

    def fast_forward(self, repo, default, sha):
        """("merged", "") | ("not_ff", msg) | ("refused", msg). Never checks anything out (#1010).

        Where the default branch is checked out (the main checkout first, else any worktree),
        `merge --ff-only` runs there. When nobody has it checked out, which is the usual case
        while other sessions keep the shared main checkout on their own branches, the ref moves
        with a compare-and-swap `update-ref`, and only forward."""
        rc, cur, _ = git(repo, "branch", "--show-current")
        where = repo if rc == 0 and cur == default else next(iter(checkouts_of(repo, default)), None)
        if where is None:
            return move_ref(repo, default, sha)
        before = dirty_checksums(where)
        rc, out, err = git(where, "merge", "--ff-only", sha)
        after = dirty_checksums(where)
        changed = [p for p, h in before.items() if after.get(p) != h]
        if changed:     # git promises this can't happen; say so loudly if it ever does
            log.error("%s: uncommitted files changed during ff-only: %s", where, changed)
        msg = _tail(f"{out}\n{err}", 8)
        if rc == 0:
            head = git(where, "rev-parse", "HEAD")[1]
            if head != sha:
                return "refused", f"after the fast-forward HEAD is {head[:10]}, expected {sha[:10]}"
            return "merged", ""
        if "not possible to fast-forward" in msg.lower():
            return "not_ff", msg
        return "refused", msg + (f"\nlocal changes: {', '.join(sorted(before))}" if before else "")

    # -- outcomes --

    def _task_state(self, pid, tid):
        return self.state.project(pid)["tasks"].setdefault(str(tid), {"failures": 0, "conflicts": 0})

    def _merged(self, ctx, entry, project, default, sha, gate, log_dir):
        pid, tid = ctx.project_id, entry["task"]
        with self.state.lock:
            p = self.state.project(pid)
            old = dict(p["baseline"])
            p["baseline"].update(gate.totals())
            p["tasks"].pop(str(tid), None)
            self.state.save()
        lines = [f"autopilot: merged `{entry['branch']}` into `{default}` as a fast-forward: "
                 f"{default} is now {sha}. Not pushed.", "", format_table(gate), ""]
        if gate.known:
            lines.append("Known failures (allowed): " + ", ".join(gate.known))
        lines.append("Flakes rerun (passed 3 solo reruns): " + (", ".join(gate.flakes) or "none"))
        counts = [f"{k} {old.get(k, 'none')} -> {v}" for k, v in gate.totals().items()]
        if counts:
            lines.append("Test count vs last green gate: " + "; ".join(counts))
        lines.append(f"Gate logs: {log_dir}")
        res = self._finish(ctx, entry, "merged", DONE, "\n".join(lines), gate=gate, commit=sha)
        if self.after_merge:
            try:
                self.after_merge(ctx, res)
            except Exception as e:
                log.exception("after_merge hook failed")
                ctx.event("error", f"task {tid}: after-merge hook failed: {type(e).__name__}: {e}")
        res["cleanup"] = self.clean_up(ctx, project, entry, default)
        return res

    def clean_up(self, ctx, project, entry, default):
        """After a merge, and after the after_merge hook (the milestone gate may run in the
        worktree): remove the task's own `ap-<id>` worktree and its branch (#1011). Returns
        "removed", "kept" or "skipped". Never forced: a worktree git refuses to remove (local
        changes, untracked or root-owned files) is kept and logged; the task is Done either way.
        Only a worktree named ap-<task> under the project's worktree root is ever touched."""
        tid, branch, wt = entry["task"], entry.get("branch", ""), entry.get("worktree", "")
        repo, root = project.repo, project.autopilot.worktree_root
        real = os.path.realpath
        if not (wt and root and os.path.basename(wt.rstrip("/")) == f"ap-{tid}"
                and os.path.dirname(real(wt)) == real(root) and real(wt) != real(repo)):
            return "skipped"
        if os.path.isdir(wt):
            rc, out, err = git(repo, "worktree", "remove", wt)
            if rc != 0 or os.path.exists(wt):
                ctx.event("merge", f"task {tid}: kept the worktree {wt}: "
                                   + (_tail(f"{out}\n{err}", 2) or "the directory is still there"))
                return "kept"
        ref = f"refs/heads/{branch}"
        rc, tip, _ = git(repo, "rev-parse", "--verify", "--quiet", ref)
        if branch and rc == 0 and not checkouts_of(repo, branch) \
                and git(repo, "merge-base", "--is-ancestor", tip, f"refs/heads/{default}")[0] == 0:
            rc, out, err = git(repo, "update-ref", "-d", ref, tip)
            if rc != 0:
                why = _tail(f"{out}\n{err}", 2)
                ctx.event("merge", f"task {tid}: kept the branch {branch}: {why}")
        ctx.event("merge", f"task {tid}: removed the worktree {wt}")
        return "removed"

    def _conflict(self, ctx, entry, default, files, project=None):
        with self.state.lock:
            ts = self._task_state(ctx.project_id, entry["task"])
            ts["conflicts"] += 1
            n = ts["conflicts"]
            self.state.save()
        listed = "\n".join(f"- `{f}`" for f in files)
        if n >= CONFLICT_LIMIT:
            return self._finish(ctx, entry, "blocked", BLOCKED,
                                f"autopilot: merge blocked: `{default}` conflicts with "
                                f"`{entry['branch']}` for the {n}th time in a row:\n{listed}\n\n"
                                "Repeated conflicts on the same files usually mean parallel "
                                "tasks overlap; consider lowering the project's cap.",
                                conflicts=files)
        return self._finish(ctx, entry, "resume", RESUME,
                            f"autopilot: resume. The merge queue could not merge `{default}` into "
                            f"`{entry['branch']}`: conflicts in\n{listed}\n\n"
                            f"Next run: in the worktree, `git merge {default}`, resolve these "
                            "conflicts keeping both sides' intent, run the pre-merge gate, commit, "
                            "and report ready to merge. The queue aborted its own merge; the "
                            f"branch is as the worker left it. (Conflict {n} of {CONFLICT_LIMIT} "
                            "before the task is Blocked.)"
                            + permissions.deny_glob_note(project, files), conflicts=files)

    def _gate_failed(self, ctx, entry, gate, log_dir):
        with self.state.lock:
            ts = self._task_state(ctx.project_id, entry["task"])
            ts["failures"] += 1
            n = ts["failures"]
            self.state.save()
        failing = "\n".join(f"- {r}" for r in gate.real)
        body = (f"Failing:\n{failing}\n\n{format_table(gate)}\n\n"
                + (f"Known failures (allowed): {', '.join(gate.known)}\n" if gate.known else "")
                + (f"Flakes that passed 3 solo reruns: {', '.join(gate.flakes)}\n" if gate.flakes else "")
                + f"Gate logs: {log_dir}")
        if n >= FAILURE_LIMIT:
            return self._finish(ctx, entry, "blocked", BLOCKED,
                                f"autopilot: merge blocked: the pre-merge gate failed with real "
                                f"failures {n} times in a row on `{entry['branch']}` (after "
                                f"merging the default branch). Not merged.\n\n{body}", gate=gate)
        return self._finish(ctx, entry, "resume", RESUME,
                            f"autopilot: resume. The pre-merge gate failed at merge time on "
                            f"`{entry['branch']}` (the default branch is merged into it now). "
                            f"Fix these, rerun the gate, commit, and report ready to merge. A "
                            f"second failure in a row blocks the task.\n\n{body}", gate=gate)

    def _resume(self, ctx, entry, why):
        return self._finish(ctx, entry, "resume", RESUME,
                            f"autopilot: resume. The merge queue did not merge `{entry.get('branch')}`: {why}")

    def _finish(self, ctx, entry, result, status, comment, gate=None, commit=None, conflicts=None):
        tid = entry["task"]
        try:
            self.tracker.move(tid, status, comment)
        except Exception as e:
            log.exception("tracker write for task %s failed", tid)
            ctx.event("error", f"task {tid}: tracker move to {status} failed: {type(e).__name__}: {e}")
        first = comment.splitlines()[0] if comment else ""
        ctx.event("merge", f"task {tid}: {result}" + (f" {commit[:10]}" if commit else "")
                  + f" -> {status}. {first[:200]}")
        res = {"task": tid, "result": result, "status": status, "branch": entry.get("branch"),
               "comment": comment}
        if commit:
            res["commit"] = commit
        if gate is not None:
            res["failing"] = gate.real
            res["flakes"] = gate.flakes
            res["suites"] = [{"suite": s.name, "ok": s.ok,
                              **({"total": s.run.total, "passed": s.run.passed,
                                  "failed": s.run.failed, "skipped": s.run.skipped}
                                 if s.run else {"exit": s.code})} for s in gate.steps]
        if conflicts:
            res["conflicts"] = conflicts
        return res
