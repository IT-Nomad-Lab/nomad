#!/usr/bin/env python3
"""nomad-autopilot supervisor: the loop that keeps agents working a project's board (PRD 5.1).

Every NOMAD_AUTOPILOT_PASS_SECS (60) seconds, or at once on POST /pass, it runs one scheduling
pass over every project whose autopilot is on. What a pass actually does is the `Scheduler`'s
job; this file owns the loop, the state, the caps and the HTTP surface.

Endpoints (127.0.0.1:NOMAD_AUTOPILOT_PORT, default 8092):
  GET  /health                     heartbeat age and an ok flag; never needs a token
  GET  /status                     every project's state, the config, spend today
  GET  /config                     budget mode, daily cap, per-project and global caps
  POST /config        {..fields}   partial update of the above
  POST /pass                       run a pass now
  POST /projects/<id>/start        autopilot on for <id>, and a pass right away
  POST /projects/<id>/stop  {"mode": "finish"|"kill"}   autopilot off; workers finish or die
  POST /projects/<id>/autopilot  {"on": true|false}      the project's ON/OFF switch (#890)

Every POST needs the `X-Nomad-Token` header, like the dispatcher. The token is generated at
~/.config/nomad/autopilot-token on first start (NOMAD_AUTOPILOT_TOKEN overrides it), and if it
cannot be read or made the daemon exits rather than serve unauthenticated.

It must not wedge silently (CLAUDE.md, "a process that never exits"):
- The heartbeat moves only when a whole pass completes, so a hung pass shows as a growing
  `heartbeat_age_s` on /health, which is what nomad-watch reads.
- An exception inside one project's pass marks that project `error` and the loop goes on.
- An exception outside any project (the state file cannot be written, say) is logged; after
  MAX_LOOP_FAILURES in a row the daemon exits 3 so systemd restarts it or shows it failed.

Plug-in points are the `Scheduler` methods. `main()` runs `autopilot.scheduler.AutopilotScheduler`:
task selection (#865), workers (#866) with the generated permission profile (#875), states and
crash recovery (#868), caps (#867). The merge queue (#871) fills `merge`.

States (PRD 5.7), decided at the end of each project's pass, first match wins:
  error          a worker ended `error` (sticky until Run is pressed again), or the pass failed:
                 nomad-pm or the repo unreachable, a bad projects.yaml entry, a hook raised
  budget_paused  the daily cap is reached and a task qualifies that could not start
  running / waiting / needs_you / idle   the Scheduler's `outcome`, else running (workers) or idle
  off            autopilot off; its last workers are still collected, nothing new starts

The switch and run mode "once" (#890). Each project has a persisted `autopilot` switch, separate
from `enabled` (running). Run reads it: ON starts the continuous run above. OFF starts run mode
"once": the first pass takes the top qualifying task, starts exactly one worker and records it in
the state (`once`: task and phase). Later passes start nothing. When that task's outcome is final
the project turns itself off with the detail "one task done: #<id> <outcome>":
  - the worker ended anything but `merge` (resume, failed, needs_operator, error): that outcome;
  - the worker ended `merge`: the merge queue's result once it reports one (merged, resume,
    blocked, needs_operator). While the task is still queued or merging the project stays on,
    `running`, starting nothing.
  - nothing qualified on the first pass: off at once, with the reason.
Budget paused, or every slot taken, before the task started: the project stays on and the next
pass tries again. The mode and the task live in the state file, so a restart resumes the same run.

Crash recovery (AC8): the first thing the loop does is `recover()`. For every project the
scheduler reconciles the stored workers and the run records on disk with the live processes
(pid plus process start time) and the `ap-<id>` worktrees. A live run is adopted and collected
when it ends; one that ended while the supervisor was down is collected on the next pass, by
its log's final result, or as `resume` when it has none.
"""
import glob
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

try:
    from . import state as st
except ImportError:  # run as a script: python3 autopilot/supervisor.py
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import state as st

log = logging.getLogger("autopilot")

HOST = "127.0.0.1"
PORT = int(os.environ.get("NOMAD_AUTOPILOT_PORT", "8092"))
PASS_SECS = float(os.environ.get("NOMAD_AUTOPILOT_PASS_SECS", "60"))
TOKEN_PATH = os.path.expanduser("~/.config/nomad/autopilot-token")
MAX_LOOP_FAILURES = 5
NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def load_or_make_token(path=TOKEN_PATH):
    """Same approach as dispatcher/dispatch.py `_load_or_make_token`: env override, else a file
    generated on first start (0600). Never fails open."""
    env = os.environ.get("NOMAD_AUTOPILOT_TOKEN")
    if env:
        return env
    try:
        if os.path.exists(path):
            tok = open(path).read().strip()
            if tok:
                return tok
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tok = secrets.token_urlsafe(24)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok)
        return tok
    except Exception as e:
        sys.exit(f"autopilot: cannot read or create {path} ({type(e).__name__}: {e}). "
                 "Refusing to serve unauthenticated; set NOMAD_AUTOPILOT_TOKEN or fix the path.")


# ---- the plug-in interface ------------------------------------------------------------------

class PassError(RuntimeError):
    """A hook's way to put the project in `error` with exactly this message as the detail
    ("nomad-pm unreachable: ...") rather than "pass failed: <exception>"."""
    plain = True

class PassContext:
    """What a Scheduler hook gets to look at during one project's pass.

    project_id  the projects.yaml id
    project     a copy of the project's stored record (state, detail, workers, events, ...)
    config      a copy of the global config (caps, budget)
    slots       how many new workers may start now; set before start_workers is called
    """

    def __init__(self, store, project_id, config, project):
        self._store = store
        self.project_id = project_id
        self.config = config
        self.project = project
        self.slots = 0

    def event(self, kind, msg):
        """Append to the project's event log (the cockpit's 'last event')."""
        self._store.event(self.project_id, kind, msg)


class Scheduler:
    """The pass, step by step (PRD 5.1). This base class is the do-nothing pass: every hook is a
    safe no-op, so the supervisor runs, keeps its heartbeat and reports `idle` before any real
    work is wired in. Later tasks subclass it (or replace methods) and pass it to Supervisor.

    A hook may raise: the supervisor records the project as `error` with the message and moves on.
    """

    def known(self, project_id):
        """Is this a project the autopilot may run? #865 checks projects.yaml here."""
        return True

    def collect(self, ctx):
        """Reap finished workers (#866). Return a list of dicts, one per finished worker:
        {"task": id, "outcome": ..., "cost_usd": float, "detail": str}, outcome one of
          merge           ready to merge; the merge queue takes it
          resume          the run ran out of time, was killed, or died with the supervisor
          needs_operator  the task waits on the operator (Awaiting approval)
          failed          the run failed, crashed or left no result marker
          error           the run's first shell command was refused and nothing got through:
                          a broken permission profile, not a bad task. The project goes to
                          `error` and starts no new worker until Run is pressed again.
        The supervisor drops them from the project's worker list and adds cost_usd to spend.
        Called for stopped projects too, until their worker list is empty."""
        return []

    def merge(self, ctx, finished):
        """Run the project's serial merge queue over what collect returned (PRD 5.5)."""

    def select_tasks(self, ctx):
        """Qualifying tasks in order (#865, PRD 5.2). Return a list of dicts, each at least
        {"task": id}. Tasks already held by a live worker must not be returned."""
        return []

    def start_workers(self, ctx, tasks):
        """Start one worker per task (#866). `tasks` is already cut to ctx.slots. Return the
        started workers as dicts, at least {"task": id}; the supervisor stores and counts them."""
        return []

    def runnable(self):
        """{project id: nomad-pm board} for every project the cockpit may offer Run on (#883).
        The base class knows no registry, so it offers nothing."""
        return {}

    def board_of(self, project_id):
        """The nomad-pm board a project's tasks live on, or None (#883: the cockpit puts the
        project's status line on that board's row)."""
        return None

    def project_cap(self, ctx):
        """This project's own worker cap (projects.yaml `cap`), or None. The supervisor uses the
        smaller of it and the cockpit's per-project cap."""
        return None

    def recover(self, ctx):
        """Startup, before the first pass: reconcile the stored workers (ctx.project["workers"])
        with what is really running. Return the worker list to store, or None to keep it."""
        return None

    def start(self, ctx):
        """Run pressed for this project."""

    def outcome(self, ctx, workers, tasks):
        """Override the project's state for this pass: return (state, detail) or None to let the
        supervisor decide (running if workers, else idle). #865 uses this for `waiting` and
        `needs_you`, naming what the project waits on."""
        return None

    def stop(self, ctx, mode):
        """Stop pressed. mode "finish": let live workers run out; "kill": kill them, keep branches."""

    def merge_result(self, ctx, task_id):
        """Run mode "once" (#890): the merge queue's result for a task whose worker ended `merge`,
        or None while the queue still holds it. The base class has no queue: nothing will merge
        it, so the worker's own outcome is final."""
        return "merge"


# ---- the supervisor -------------------------------------------------------------------------

class Supervisor:
    def __init__(self, store, scheduler=None, pass_secs=PASS_SECS, budget=None, alerter=None,
                 digest=None):
        """budget: budget.Budget (None = the store's plain spend count); alerter: alerts.Alerter
        (None = no alerts); digest: digest.Digest, ticked once per pass (None = no digest).
        main() passes all three."""
        self.store = store
        self.scheduler = scheduler or Scheduler()
        self.budget = budget
        self.alerter = alerter
        self.digest = digest
        self.pass_secs = pass_secs
        self.heartbeat = time.time()     # start counts as a beat; the first pass runs at once
        self.passes = 0
        self.loop_failures = 0
        self.fatal = None                # set when the loop gives up; main() exits 3
        self.recovered = False
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._pass_lock = threading.Lock()

    # -- one pass --

    def _ctx(self, pid):
        return PassContext(self.store, pid, self.store.config(), self.store.project(pid))

    def _slots(self, pid, cfg, cap=None):
        """Free worker slots for `pid`: under its cap (default the cockpit's per-project cap)
        and under the global cap, counting every project's workers, stopped ones included."""
        snap = self.store.snapshot()["projects"]
        mine = len(snap.get(pid, {}).get("workers", []))
        total = sum(len(p.get("workers", [])) for p in snap.values())
        cap = cfg["per_project_cap"] if cap is None else cap
        return max(0, min(cap - mine, cfg["global_cap"] - total))

    def budget_exhausted(self, cfg):
        return cfg["budget_mode"] == "cap" and self.store.spent_today() >= cfg["daily_cap_usd"]

    def _record_finished(self, ctx, workers, finished):
        """Drop finished workers from the list, book their cost, log them, raise a fault."""
        done = {str(f.get("task")) for f in finished}
        workers = [w for w in workers if str(w.get("task")) not in done]
        for f in finished:
            if self.budget is not None:
                self.budget.record(ctx.project_id, f)
            elif f.get("cost_usd"):
                self.store.add_spend(f["cost_usd"])
            outcome, detail = f.get("outcome", "?"), f.get("detail") or ""
            ctx.event("finished", f"task {f.get('task')}: {outcome}" + (f" ({detail})" if detail else ""))
            if outcome == "error":
                self.store.set_fault(ctx.project_id, f"task {f.get('task')}: {detail or 'worker error'}")
        return workers

    def _collect(self, ctx):
        """Reap finished workers and hand them to the merge queue. merge is called on every pass,
        finished or not, so a queue left by a restart is worked without waiting for a worker."""
        sch = self.scheduler
        workers = list(ctx.project["workers"])
        finished = sch.collect(ctx) or []
        if finished:
            workers = self._record_finished(ctx, workers, finished)
            self.store.finish_pass(ctx.project_id, ctx.project["state"], ctx.project["detail"], workers)
            ctx.project["workers"] = list(workers)      # later hooks see the reaped list
        sch.merge(ctx, finished)
        return workers, finished

    def project_cap(self, ctx, cfg):
        own = self.scheduler.project_cap(ctx)
        return cfg["per_project_cap"] if own is None else min(cfg["per_project_cap"], own)

    def run_pass(self, pid):
        """One project's pass. Raises on a hook failure; run_all turns that into `error`."""
        sch = self.scheduler
        ctx = self._ctx(pid)
        workers, finished = self._collect(ctx)
        if ctx.project.get("run_mode") == "once":
            return self._once_pass(ctx, workers, finished)
        started = []

        fault = self.store.project(pid).get("fault")
        if fault:
            n = f"; {len(workers)} worker(s) finishing" if workers else ""
            detail = f"{fault}. Fix it, then press Run again{n}"
            self.store.finish_pass(pid, "error", detail, workers)
            self._alert(pid, finished, started, "error", detail)
            return

        cfg = self.store.config()
        if self.budget is not None:
            decision = self.budget.check()
            paused = not decision.allowed
        else:
            decision, paused = None, self.budget_exhausted(cfg)
        tasks = sch.select_tasks(ctx) or []
        cap = self.project_cap(ctx, cfg)
        ctx.slots = 0 if paused else self._slots(pid, cfg, cap)
        if tasks and ctx.slots:
            started = list(sch.start_workers(ctx, tasks[:ctx.slots]) or [])
            for w in started:
                ctx.event("started", f"task {w.get('task')}")
            workers = workers + list(started)

        held = {str(w.get("task")) for w in workers}
        waiting = [t for t in tasks if str(t.get("task")) not in held]
        if paused and waiting:
            state, detail = "budget_paused", (
                f"daily cap ${cfg['daily_cap_usd']:.2f} reached; {len(waiting)} task(s) wait; "
                "resumes at midnight or when the budget is raised"
                + (f"; {len(workers)} worker(s) finishing" if workers else ""))
            if self.budget is not None:
                self.budget.mark_paused(pid)
        else:
            decided = sch.outcome(ctx, workers, tasks)
            if decided:
                state, detail = decided
            elif workers:
                state, detail = "running", f"{len(workers)} worker(s): " + ", ".join(
                    f"#{w.get('task')}" for w in workers)
            else:
                state, detail = "idle", "nothing to do"
        self.store.finish_pass(pid, state, detail, workers)
        self._alert(pid, finished, started, state, detail)

    # -- run mode "once" (#890) --

    def _once_done(self, pid, finished, detail):
        self.store.finish_once(pid, detail)
        self._alert(pid, finished, [], "off", detail)

    def _once_pass(self, ctx, workers, finished):
        """A pass of a run_mode "once" run: start the one task, or follow it to a final outcome."""
        sch, pid = self.scheduler, ctx.project_id
        once = ctx.project.get("once")
        if once:
            tid = str(once["task"])
            f = next((f for f in finished if str(f.get("task")) == tid), None)
            if f is not None:
                outcome = f.get("outcome") or "unknown"
                if outcome != "merge":
                    return self._once_done(pid, finished, f"one task done: #{tid} {outcome}")
                once = dict(once, phase="merging")
                self.store.set_once(pid, once)
            if once.get("phase") == "merging":
                result = sch.merge_result(ctx, tid)
                if result is None:
                    detail = f"one task #{tid}: in the merge queue"
                    self.store.finish_pass(pid, "running", detail, workers)
                    return self._alert(pid, finished, [], "running", detail)
                return self._once_done(pid, finished, f"one task done: #{tid} {result}")
            if any(str(w.get("task")) == tid for w in workers):
                detail = f"one task #{tid}: 1 worker"
                self.store.finish_pass(pid, "running", detail, workers)
                return self._alert(pid, finished, [], "running", detail)
            return self._once_done(pid, finished,
                                   f"one task done: #{tid} lost (no worker and no outcome)")

        fault = self.store.project(pid).get("fault")
        if fault:
            detail = f"{fault}. Fix it, then press Run again"
            self.store.finish_pass(pid, "error", detail, workers)
            return self._alert(pid, finished, [], "error", detail)
        cfg = self.store.config()
        paused = (not self.budget.check().allowed) if self.budget is not None \
            else self.budget_exhausted(cfg)
        tasks = sch.select_tasks(ctx) or []
        if not tasks:
            ctx.slots = 0           # the reason only: no criteria draft is started for it
            decided = sch.outcome(ctx, [], [])
            why = decided[1] if decided else "nothing to do"
            return self._once_done(pid, finished, f"one task: nothing qualifies ({why})")
        top = str(tasks[0].get("task"))
        if paused:
            detail = (f"daily cap ${cfg['daily_cap_usd']:.2f} reached; one task: #{top} waits; "
                      "resumes at midnight or when the budget is raised")
            if self.budget is not None:
                self.budget.mark_paused(pid)
            self.store.finish_pass(pid, "budget_paused", detail, workers)
            return self._alert(pid, finished, [], "budget_paused", detail)
        ctx.slots = min(1, self._slots(pid, cfg, self.project_cap(ctx, cfg)))
        if not ctx.slots:
            detail = f"one task: #{top} ready, every worker slot is taken"
            self.store.finish_pass(pid, "waiting", detail, workers)
            return self._alert(pid, finished, [], "waiting", detail)
        started = list(sch.start_workers(ctx, tasks[:1]) or [])[:1]
        if not started:
            errors = (getattr(getattr(sch, "pool", None), "start_errors", None) or {}).get(pid) or []
            detail = f"one task: no worker could start for #{top}" + (
                ": " + "; ".join(errors) if errors else "")
            self.store.finish_pass(pid, "error", detail, workers)
            return self._alert(pid, finished, [], "error", detail)
        w = started[0]
        tid = str(w.get("task"))
        self.store.set_once(pid, {"task": tid, "phase": "working"})
        ctx.event("started", f"task {tid} (one task)")
        detail = f"one task #{tid}: 1 worker"
        self.store.finish_pass(pid, "running", detail, workers + started)
        self._alert(pid, finished, started, "running", detail)

    def _alert(self, pid, finished, started, state, detail):
        if self.alerter is not None:
            self.alerter.on_pass(pid, finished=finished, started=started, state=state, detail=detail)

    def drain(self, pid):
        """A stopped project's pass: collect its last workers, start nothing."""
        ctx = self._ctx(pid)
        workers, _ = self._collect(ctx)
        detail = ctx.project["detail"].split(";")[0].split(",")[0] or "stopped"
        detail += (f"; {len(workers)} worker(s) still finishing" if workers
                   else "; all workers collected")
        self.store.finish_pass(pid, "off", detail, workers)

    def run_all(self):
        """A pass over every enabled project, and a collect-only pass over every stopped project
        that still has workers. One project's failure never stops the others."""
        with self._pass_lock:
            if self.budget is not None:
                try:
                    self.budget.reconcile()
                except Exception:
                    log.exception("budget reconcile failed")
            snap = self.store.snapshot()["projects"]
            draining = sorted(pid for pid, p in snap.items() if not p["enabled"] and p["workers"])
            for pid in draining:
                try:
                    self.drain(pid)
                except Exception as e:
                    log.exception("collect failed for stopped %s", pid)
                    self.store.event(pid, "error", f"collect failed: {type(e).__name__}: {e}")
            for pid in self.store.enabled_projects():
                try:
                    self.run_pass(pid)
                except Exception as e:
                    detail = str(e) if getattr(e, "plain", False) else f"pass failed: {type(e).__name__}: {e}"
                    if not getattr(e, "plain", False):
                        log.exception("pass failed for %s", pid)
                    else:
                        log.warning("%s: %s", pid, detail)
                    self.store.finish_pass(pid, "error", detail)
                    if self.alerter is not None:
                        self.alerter.project_state(pid, "error", detail)
            if self.digest is not None:
                try:
                    self.digest.tick()
                except Exception:
                    log.exception("digest failed")
            self.passes += 1
            self.heartbeat = time.time()

    def recover(self):
        """Once at startup: every stored project's workers reconciled by the scheduler."""
        for pid in sorted(self.store.snapshot()["projects"]):
            ctx = self._ctx(pid)
            try:
                workers = self.scheduler.recover(ctx)
            except Exception as e:
                log.exception("recovery failed for %s", pid)
                self.store.event(pid, "error", f"recovery failed: {type(e).__name__}: {e}")
                continue
            if workers is None:
                continue
            before = {str(w.get("task")) for w in ctx.project["workers"]}
            self.store.set_workers(pid, workers)
            if before or workers:
                self.store.event(pid, "recover", f"supervisor restarted: {len(before)} stored worker(s), "
                                                 f"{len(workers)} to collect")
        self.recovered = True

    # -- the loop --

    def loop(self):
        while not self._halt.is_set():
            self._wake.clear()          # before the pass, so a /pass arriving during it is kept
            try:
                if not self.recovered:
                    self.recover()
                self.run_all()
                self.loop_failures = 0
            except Exception as e:
                self.loop_failures += 1
                log.exception("scheduling loop failure %d/%d", self.loop_failures, MAX_LOOP_FAILURES)
                if self.loop_failures >= MAX_LOOP_FAILURES:
                    self.fatal = f"{self.loop_failures} loop failures in a row: {type(e).__name__}: {e}"
                    log.error("giving up: %s", self.fatal)
                    self._halt.set()
                    return
            self._wake.wait(self.pass_secs)

    def trigger(self):
        self._wake.set()

    def halt(self):
        self._halt.set()
        self._wake.set()

    # -- what the HTTP routes call --

    def health(self):
        age = time.time() - self.heartbeat
        return {"ok": self.fatal is None and age <= 3 * self.pass_secs,
                "heartbeat_age_s": round(age, 1), "pass_secs": self.pass_secs,
                "passes": self.passes, "fatal": self.fatal}

    def status(self):
        snap = self.store.snapshot()
        out = {"ok": True, "heartbeat_age_s": round(time.time() - self.heartbeat, 1),
               "config": snap["config"], "spend": snap["spend"], "projects": snap["projects"],
               "budget": self.budget.status() if self.budget is not None else None}
        # For the cockpit (#883): which projects get a Run button, and which board row each
        # project's status line goes on. A registry that cannot be read offers no Run button and
        # says why; it never breaks /status.
        runnable, boards = {}, {}
        try:
            runnable = dict(self.scheduler.runnable() or {})
        except Exception as e:
            log.warning("runnable projects unavailable: %s", e)
            out["runnable_error"] = f"{type(e).__name__}: {e}"[:300]
        for pid in snap["projects"]:
            try:
                boards[pid] = self.scheduler.board_of(pid)
            except Exception:
                boards[pid] = None
        boards.update(runnable)
        out["runnable"] = sorted(runnable)
        out["boards"] = boards
        return out

    def runs(self, limit=None):
        """Recent worker runs from the `runs/` dir beside state.json (where worker.py writes)."""
        return recent_runs(os.path.join(os.path.dirname(self.store.path), "runs"),
                           limit or RUNS_DEFAULT)

    def start_project(self, pid):
        self.store.start(pid)
        try:
            self.scheduler.start(self._ctx(pid))
        except Exception as e:
            log.exception("start hook failed for %s", pid)
            self.store.event(pid, "error", f"start failed: {type(e).__name__}: {e}")
        self.trigger()
        return self.store.project(pid)

    def set_autopilot(self, pid, on):
        self.store.set_autopilot(pid, on)
        return self.store.project(pid)

    def stop_project(self, pid, mode):
        self.store.stop(pid, mode)
        try:
            self.scheduler.stop(self._ctx(pid), mode)
        except Exception as e:
            log.exception("stop hook failed for %s", pid)
            self.store.event(pid, "error", f"stop ({mode}) failed: {type(e).__name__}: {e}")
        return self.store.project(pid)


# ---- worker runs, for the cockpit's runs table (#955) --------------------------------------------

RUNS_DEFAULT, RUNS_MAX = 50, 200
RESULT_TAIL_BYTES = 256 * 1024       # the final `result` event is the log's last line
SUMMARY_CHARS = 280
MARKER_LINE = re.compile(r"^\s*autopilot:\s*result\b.*$", re.I | re.M)


def _result_event(log_path):
    """The final stream-json `result` event of a worker log, or None. Reads only the tail."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - RESULT_TAIL_BYTES))
            chunk = f.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return None
    for line in reversed(chunk.splitlines()):
        if '"result"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            return ev
    return None


def _summary(text):
    """The worker's last paragraph, without the `autopilot: result` marker, cut to one line."""
    paras = [p.strip() for p in MARKER_LINE.sub("", text or "").split("\n\n") if p.strip()]
    s = " ".join(paras[-1].split()) if paras else ""
    return s if len(s) <= SUMMARY_CHARS else s[:SUMMARY_CHARS - 1].rstrip() + "…"


def recent_runs(runs_dir, limit=RUNS_DEFAULT, now=None):
    """The newest `limit` worker runs across projects, from the `.run.json` records workers
    write (worker.py). `outcome` is what collect decided (merge, failed, needs_operator,
    resume, ...), `running` while the run is uncollected and inside its deadline, and
    `unknown` for an uncollected record past it (the supervisor collects it on a later pass).
    A record that can't be read is skipped; it never breaks the list."""
    now = time.time() if now is None else now
    recs = []
    for f in glob.glob(os.path.join(runs_dir, "*", "*.run.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if (isinstance(rec, dict) and rec.get("task") is not None
                and isinstance(rec.get("started"), (int, float))):
            recs.append((rec, f))
    recs.sort(key=lambda rf: rf[0]["started"], reverse=True)
    out = []
    for rec, f in recs[:limit]:
        outcome = rec.get("collected")
        if not outcome:
            dl = rec.get("deadline")
            outcome = "running" if not isinstance(dl, (int, float)) or now < dl else "unknown"
        ev = _result_event(rec.get("log")) if outcome not in ("running",) else None
        cost = ev.get("total_cost_usd") if ev else None
        out.append({
            "id": os.path.basename(f)[:-len(".run.json")],
            "project": rec.get("project") or os.path.basename(os.path.dirname(f)),
            "task": str(rec["task"]),
            "branch": rec.get("branch") or "",
            "started": rec["started"],
            "finished": rec.get("collected_at"),
            "outcome": outcome,
            "summary": _summary(ev.get("result")) if ev else "",
            "cost_usd": round(cost, 4) if isinstance(cost, (int, float)) else None,
        })
    return out


# ---- HTTP -----------------------------------------------------------------------------------

def make_handler(sup, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            log.debug("%s " + fmt, self.address_string(), *a)

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            data = json.loads(self.rfile.read(n))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, sup.health())
            if self.path == "/status":
                return self._send(200, sup.status())
            if self.path == "/config":
                return self._send(200, {"ok": True, "config": sup.store.config()})
            url = urlsplit(self.path)
            if url.path == "/runs":
                try:
                    n = int((parse_qs(url.query).get("limit") or [RUNS_DEFAULT])[0])
                except ValueError:
                    return self._send(400, {"ok": False, "error": "limit must be a number"})
                return self._send(200, {"ok": True, "runs": sup.runs(max(1, min(n, RUNS_MAX)))})
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("X-Nomad-Token", ""), token):
                return self._send(401, {"ok": False, "error": "missing or wrong X-Nomad-Token"})
            try:
                body = self._body()
            except ValueError as e:
                return self._send(400, {"ok": False, "error": f"bad JSON body: {e}"})
            try:
                if self.path == "/pass":
                    sup.trigger()
                    return self._send(202, {"ok": True, "queued": True})
                if self.path == "/config":
                    return self._send(200, {"ok": True, "config": sup.store.update_config(body)})
                m = re.fullmatch(r"/projects/([^/]+)/(start|stop|autopilot)", self.path)
                if m:
                    pid, action = m.groups()
                    if not PROJECT_ID.match(pid) or not sup.scheduler.known(pid):
                        return self._send(404, {"ok": False, "error": f"unknown project {pid!r}"})
                    if action == "autopilot":
                        on = body.get("on")
                        if not isinstance(on, bool):
                            return self._send(400, {"ok": False, "error": 'body must be {"on": true|false}'})
                        return self._send(200, {"ok": True, "project": sup.set_autopilot(pid, on)})
                    if action == "start":
                        return self._send(200, {"ok": True, "project": sup.start_project(pid)})
                    mode = body.get("mode", "finish")
                    if mode not in st.STOP_MODES:
                        return self._send(400, {"ok": False, "error": f"mode must be one of {st.STOP_MODES}"})
                    return self._send(200, {"ok": True, "project": sup.stop_project(pid, mode)})
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            self._send(404, {"ok": False, "error": "not found"})

    return Handler


def make_server(sup, token, host=HOST, port=PORT):
    return ThreadingHTTPServer((host, port), make_handler(sup, token))


def milestone_alert(alerter):
    """The milestone gate's alert(ctx, kind, text) sent through the Alerter, one key per text."""
    import hashlib

    def alert(ctx, kind, text):
        digest = hashlib.sha1(text.encode()).hexdigest()[:12]
        alerter.notify(ctx.project_id, f"{kind}:{ctx.project_id}:{digest}",
                       f"{ctx.project_id}: {kind.replace('_', ' ')}", text)
    return alert


def build_scheduler(alerter=None):
    """The real pass: task selection, workers, generated permissions, recovery, caps, the merge
    queue and the milestone gate after each merge, and criteria drafts for New tasks (#889)."""
    if NOMAD not in sys.path:
        sys.path.insert(0, NOMAD)
    from autopilot import projects
    from autopilot.merge import MergeQueue
    from autopilot.milestone import with_milestones
    from autopilot.scheduler import AutopilotScheduler
    from autopilot.criteria import CriteriaDrafter
    from autopilot.tasks import PMClient
    queue = MergeQueue(project_of=projects.hub_get)
    with_milestones(queue, alert=milestone_alert(alerter) if alerter is not None else None)
    pm = PMClient()
    return AutopilotScheduler(pm=pm, merger=queue, drafter=CriteriaDrafter(pm))


def main():
    logging.basicConfig(level=os.environ.get("NOMAD_AUTOPILOT_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = load_or_make_token()
    if NOMAD not in sys.path:
        sys.path.insert(0, NOMAD)
    from autopilot import alerts, budget, digest
    store = st.StateStore()
    alerter = alerts.from_env(store)
    sch, bud = build_scheduler(alerter), budget.Budget(store)
    sup = Supervisor(store, sch, budget=bud, alerter=alerter,
                     digest=digest.Digest(store, alerter, scheduler=sch, budget=bud))
    srv = make_server(sup, token)

    def run_loop():
        sup.loop()
        if sup.fatal:
            srv.shutdown()      # unblocks serve_forever so main can exit non-zero

    threading.Thread(target=run_loop, name="autopilot-loop", daemon=True).start()
    log.info("nomad-autopilot on %s:%d, pass every %ss", HOST, PORT, sup.pass_secs)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sup.halt()
        srv.server_close()
    if sup.fatal:
        sys.exit(3)


if __name__ == "__main__":
    # Run as a script, this file is `__main__`; hand over to the package module so every part
    # of the autopilot shares one copy of each class.
    if NOMAD not in sys.path:
        sys.path.insert(0, NOMAD)
    from autopilot.supervisor import main as _main
    _main()
