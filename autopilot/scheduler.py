"""The real scheduling pass: task selection (#865) + workers (#866) with the generated permission
profile (#875), project states and crash recovery (#868), caps (#867). `supervisor.main()` runs it.

    sup = Supervisor(StateStore(), AutopilotScheduler())

What each hook does here:
  known          projects.yaml (the hub's, see projects.hub_get) has an autopilot block and a board
  runnable       every such project with its board, for the cockpit's Run buttons (#883)
  collect        reap runs (WorkerPool.collect); a `resume` the supervisor decided (time limit,
                 operator kill, died with the supervisor) is written to the task as an
                 `autopilot: resume ...` comment, so selection picks the task up again;
                 a `failed`, `needs_operator` or `error` task is benched (see below)
  select_tasks   the repo must be reachable, then nomad-pm is read (TaskSelection); either
                 failing puts the project in `error` naming which. Benched tasks are held back.
  start_workers  WorkerPool.start_workers; a projects.yaml entry the permission generator
                 refuses puts the project in `error`
  project_cap    projects.yaml `cap` (default 3); the supervisor takes the smaller of it and the
                 cockpit's per-project cap, and the global cap (6) over all projects
  outcome        the state and its detail from the Selection and the workers (PRD 5.7)
  recover        WorkerPool.recover: stored workers and run records vs live processes
  start          Run pressed: the bench is cleared
  merge_result   run mode "once" (#890): None while the merge queue holds the task, then the
                 queue's result for it (merged, resume, blocked, needs_operator)

Criteria drafts (#889, autopilot/criteria.py), when a `drafter` is given (build_scheduler does):
select_tasks first posts every finished draft, so this pass's selection sees it; outcome, which
runs after workers started, launches at most one new draft for a `criteria` task, and only when
the pass could start workers (ctx.slots > 0: not budget paused, a slot free). The idle detail
names drafts in flight.

The bench. A run that ended `failed`, `needs_operator` or `error` normally left its task Blocked
or Awaiting approval, so selection skips it anyway. If it did not (it crashed before writing to
the tracker, or never got a shell), the task would still be Ready and would start again on every
pass, spending money in a loop. So such a task is held back, and shown as needing the operator,
until the operator presses Run again or restarts the supervisor. The bench lives in memory.

Shared locks (PRD 5.4) are not taken here: each command wrapper permissions.write generates takes
its command's locks, plus the GPU lock (/tmp/nomad-gpu.lock) for test and build runs of a
`requires_gpu` project, with flock for the whole run. Two workers reaching the same lock queue on
it; everything else they do runs in parallel.
"""
import logging
import os

from . import projects
from . import tasks as tk
from . import worker as wk
from .supervisor import PassError, Scheduler

log = logging.getLogger("autopilot.scheduler")

BENCH = ("failed", "needs_operator", "error")


class AutopilotScheduler(tk.TaskSelection, Scheduler):
    """pm: nomad-pm client (tasks.PMClient); registry: project id -> projects.Project
    (projects.hub_get); pool: worker.WorkerPool; merger: callable(ctx, finished) for the merge
    queue (#871), None = no merges yet; drafter: criteria.CriteriaDrafter (#889), None = New
    tasks without criteria are only reported."""

    def __init__(self, pm=None, registry=None, pool=None, merger=None, drafter=None,
                 listing=None):
        registry = registry or projects.hub_get
        self.listing = listing or projects.hub_load    # every registry entry, for runnable()
        super().__init__(pm=pm, registry=registry)
        self.pool = pool or wk.WorkerPool(project_of=registry, tracker=wk.tracker_reader())
        self.merger = merger
        self.drafter = drafter
        self.benched = {}       # project id -> {task id (str): reason}

    # -- helpers --

    def _project(self, pid):
        p = self._entry(pid)
        if p is None:
            raise PassError(f"project {pid!r} is not in {projects.hub_file()}")
        return p

    def _bench(self, pid):
        return self.benched.setdefault(pid, {})

    # -- hooks --

    def runnable(self):
        """Every hub project with an `autopilot:` block and a board (what known() accepts)."""
        return {p.id: tk.board_for(p.id) for p in self.listing()
                if p.autopilot and tk.board_for(p.id)}

    def board_of(self, project_id):
        return tk.board_for(project_id)

    def project_cap(self, ctx):
        p = self._entry(ctx.project_id)
        return p.autopilot.cap if p is not None and p.autopilot else None

    def start(self, ctx):
        self.benched.pop(ctx.project_id, None)

    def recover(self, ctx):
        return self.pool.recover(ctx, ctx.project.get("workers", []))

    def collect(self, ctx):
        finished = self.pool.collect(ctx)
        for f in finished:
            tid = str(f.get("task"))
            if f.get("outcome") in BENCH:
                self._bench(ctx.project_id)[tid] = f"last run {f['outcome']}" + (
                    f": {f['detail']}" if f.get("detail") else "")
            if f.get("requeue"):
                self._note_resume(ctx, tid, f)
        return finished

    def _note_resume(self, ctx, tid, f):
        write = getattr(self.pm, "comment", None)
        if write is None:
            return
        text = (f"autopilot: resume. {f.get('detail') or 'run stopped'}. The branch "
                f"`{f.get('branch')}` and worktree `{f.get('worktree')}` are kept; the next run "
                "continues from the last commit there.")
        try:
            write(int(tid), text)
        except Exception as e:
            ctx.event("error", f"task {tid}: could not comment `autopilot: resume`: {e}")

    def merge(self, ctx, finished):
        """merger is a merge.MergeQueue or a plain callable(ctx, finished)."""
        if self.merger:
            getattr(self.merger, "merge", self.merger)(ctx, finished)

    def _merging(self, pid):
        """Task ids the merge queue holds for pid (queued or merging)."""
        busy = getattr(self.merger, "busy", None)
        return {str(t) for t in busy(pid)} if busy else set()

    def merge_result(self, ctx, task_id):
        """#890: None while the queue holds the task, else its final result. A plain callable
        merger (tests, no queue object) reports nothing, so the worker's `merge` is final."""
        tid = str(task_id)
        if tid in self._merging(ctx.project_id):
            return None
        result = getattr(self.merger, "result", None)
        if result is None:
            return "merge"
        return result(ctx.project_id, tid) or "merge result unknown (the queue kept no record)"

    def select_tasks(self, ctx):
        p = self._project(ctx.project_id)
        repo = p.repo
        if not os.path.isdir(repo) or wk.git(repo, "rev-parse", "--is-inside-work-tree") != "true":
            raise PassError(f"repo unreachable: {repo} is not a readable git checkout")
        if not tk.board_for(ctx.project_id):
            raise PassError(f"project {ctx.project_id!r} has no nomad-pm board")
        if self.drafter is not None:
            self.drafter.collect(ctx)
        try:
            qualified = super().select_tasks(ctx)
        except Exception as e:
            raise PassError(f"nomad-pm unreachable: {type(e).__name__}: {e}") from e
        bench = self._bench(ctx.project_id)
        merging = self._merging(ctx.project_id)
        sel = self.selections.get(ctx.project_id)
        if not (bench or merging) or sel is None:
            return qualified
        keep = []
        for t in sel.qualified:
            if str(t.id) in merging:
                sel.skipped.append(tk.Skipped(t, "running", "in the merge queue"))
            elif str(t.id) in bench:
                sel.skipped.append(tk.Skipped(t, "operator", bench[str(t.id)] +
                                              "; press Run again to retry it"))
            else:
                keep.append(t)
        sel.qualified[:] = keep
        sel.skipped.sort(key=lambda s: s.task.id)
        return [t.as_dict() for t in keep]

    def start_workers(self, ctx, tasks):
        try:
            return self.pool.start_workers(ctx, tasks)
        except projects.ConfigError as e:
            # a projects.yaml entry that would widen the template, or no nomad-pm to hand over
            raise PassError(f"worker profile not generated: {e}") from e

    def stop(self, ctx, mode):
        return self.pool.stop(ctx, mode)

    def outcome(self, ctx, workers, tasks):
        """(state, detail) from the Selection and the workers:
        running    n workers, named, plus what else waits (needs you, blocked, queued on a cap)
        needs_you  no workers, nothing qualifies, some task waits on the operator
        waiting    no workers, nothing qualifies, some task waits on another task; or tasks
                   qualify but every slot is taken (the global cap is full with other projects)
        error      tasks qualify, slots were free, and no worker could start
        idle       nothing the autopilot may do, with what it leaves alone
        budget_paused is the supervisor's, decided before this is asked."""
        sel = self.selections.get(ctx.project_id)
        if sel is None:
            return None
        if self.drafter is not None and ctx.slots > 0:
            self.drafter.draft(ctx, sel)
        held = {str(w.get("task")) for w in workers}
        queued = [t for t in tasks if str(t.get("task")) not in held]
        ops, waits = sel.of_kind("operator"), sel.of_kind("waiting")
        merging = sorted(self._merging(ctx.project_id), key=lambda t: (len(t), t))
        if merging and not workers and not queued:
            return "running", "merging: " + ", ".join(f"#{t}" for t in merging)
        if workers:
            parts = [f"{len(workers)} worker(s): " + ", ".join(f"#{w.get('task')}" for w in workers)]
            if merging:
                parts.append("merging: " + ", ".join(f"#{t}" for t in merging))
            if queued:
                parts.append(f"{len(queued)} more queued on the cap: "
                             + ", ".join(f"#{t['task']}" for t in queued))
            if ops:
                parts.append("needs you: " + ", ".join(f"#{s.task.id} {s.reason}" for s in ops))
            if waits:
                parts.append("waiting: " + ", ".join(f"#{s.task.id} {s.reason}" for s in waits))
            return "running", "; ".join(parts)
        if queued:
            errors = self.pool.start_errors.get(ctx.project_id) or []
            if ctx.slots and errors:
                return "error", "no worker could start: " + "; ".join(errors)
            if not ctx.slots:
                return "waiting", (f"{len(queued)} task(s) ready, every worker slot is taken "
                                   "(global cap)")
            return None
        base = tk.outcome_for(sel, workers)
        if base:
            return base
        left = []
        crit = sel.of_kind("criteria")
        drafting = set(self.drafter.pending(ctx.project_id)) if self.drafter is not None else set()
        if crit:
            left.append(f"{len(crit)} New without acceptance criteria: "
                        + ", ".join(f"#{s.task.id}" + (" (drafting)" if s.task.id in drafting else "")
                                    for s in crit))
        person = sel.of_kind("person")
        if person:
            left.append(f"{len(person)} person-only: " + ", ".join(f"#{s.task.id}" for s in person))
        return "idle", "nothing to do" + (f" ({'; '.join(left)})" if left else "")
