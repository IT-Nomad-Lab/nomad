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
until the operator answers on the board (#1025: its status moves, or a `grant:` line, an operator
comment or a status move arrives after it was benched), presses Run again, or restarts the
supervisor. A task nobody touched stays benched, so a failing task doesn't loop. The bench lives
in memory.

Shared locks (PRD 5.4) are not taken here: each command wrapper permissions.write generates takes
its command's locks, plus the GPU lock (/tmp/nomad-gpu.lock) for test and build runs of a
`requires_gpu` project, with flock for the whole run. Two workers reaching the same lock queue on
it; everything else they do runs in parallel.
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import projects
from . import state as st
from . import tasks as tk
from . import worker as wk
from .supervisor import PassError, Scheduler

log = logging.getLogger("autopilot.scheduler")

BENCH = ("failed", "needs_operator", "error")


@dataclass
class Benched:
    """Why a task is held back, when (UTC), and its status at the first pass after (#1025)."""
    reason: str
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = None


def _after(ts, since):
    """True when the ISO 8601 timestamp `ts` (naive = UTC) is later than `since`."""
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)) > since


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
        self.benched = {}       # project id -> {task id (str): Benched}

    # -- helpers --

    def _project(self, pid):
        p = self._entry(pid)
        if p is None:
            raise PassError(f"project {pid!r} is not in {projects.hub_file()}")
        return p

    def _bench(self, pid):
        return self.benched.setdefault(pid, {})

    def _release(self, ctx, sel, bench):
        """#1025: take a task off the bench once the operator answered on the board since it was
        benched: its status moved, or a `grant:` line, an operator comment or a status move
        arrived after `since`. The first pass that sees a benched task records its status."""
        for t in [*sel.qualified, *(s.task for s in sel.skipped)]:
            b = bench.get(str(t.id))
            if b is not None and b.status is None:
                b.status = t.status
        for t in sel.qualified:
            b = bench.get(str(t.id))
            why = b and self._answered(t, b)
            if why:
                del bench[str(t.id)]
                ctx.event("bench", f"task {t.id}: off the bench, {why}")

    def _answered(self, t, b):
        """What the operator did since the task was benched, or None."""
        if t.status != b.status:
            return f"status moved from {b.status} to {t.status}"
        for g in t.grants:
            if _after(g.created, b.since):
                return f"new `{g.line}`"
        read = getattr(self.pm, "activities", None)
        if read is None:
            return None
        who = getattr(self.pm, "manager_id", None)
        manager_id = who() if who else None
        for a in read(t.id):
            if not _after(a.get("createdAt"), b.since):
                continue
            moved = tk._moved_to(a)
            if moved:
                return f"moved to {moved}"
            if tk._counted_text(a, tk.OPERATOR_ID, manager_id):
                return "new operator comment"
        return None

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
                self._bench(ctx.project_id)[tid] = Benched(f"last run {f['outcome']}" + (
                    f": {f['detail']}" if f.get("detail") else ""))
            if f.get("requeue"):
                self._note_resume(ctx, tid, f)
            if f.get("config_changes"):
                self._note_config(ctx, tid, f)
            if f.get("outcome") == "failed":
                self._escalate(ctx, tid, f)
        return finished

    def _escalate(self, ctx, tid, f):
        """#1109: the 3rd failed run in a row of one task sends it to the operator instead of a
        4th run. The streak comes from the cost ledger, so a restart does not reset it."""
        state_dir = getattr(self.pool, "state_dir", None)
        if not state_dir:
            return
        n = st.failed_streak(state_dir, ctx.project_id, tid)
        if n < st.ESCALATE_AFTER:
            return
        text = (f"autopilot: needs operator. {n} runs in a row failed; the last one: "
                f"{f.get('detail') or 'no detail'}. No further run until you answer: move the "
                "task to Approved to retry it (add what changed in a comment), or to Changes "
                "requested with what to do differently."
                + (f" Branch `{f['branch']}` is kept." if f.get("branch") else ""))
        try:
            self.pm.comment(int(tid), text)
            try:
                self.pm.transition(int(tid), "Awaiting approval")
            except Exception:
                # Blocked and Ready have no direct move to Awaiting approval (nomad-pm seed.rb)
                self.pm.transition(int(tid), "In Progress")
                self.pm.transition(int(tid), "Awaiting approval")
        except Exception as e:
            ctx.event("error", f"task {tid}: could not escalate after {n} failed runs: {e}")
            return
        st.note_escalated(state_dir, ctx.project_id, tid)
        ctx.event("bench", f"task {tid}: {n} failed runs in a row; sent to the operator")

    def _note_config(self, ctx, tid, f):
        """#979, PRD 5.6: the run reported ready to merge but changed agent config no grant
        named. The branch isn't queued; the task goes to Awaiting approval with the paths."""
        listed = "\n".join(f"- `{p}`" for p in f["config_changes"])
        text = (f"autopilot: needs operator. The run on `{f.get('branch')}` reported ready to "
                f"merge, but it changed agent config that no grant named:\n{listed}\n\nNot "
                "queued for merge. To accept the change, comment `grant: edit <path>` for each "
                "path and move the task to Ready; otherwise ask for it to be reverted."
                + wk._permissions().deny_glob_note(self._entry(ctx.project_id),
                                                   f["config_changes"]))
        try:
            self.pm.comment(int(tid), text)
            self.pm.transition(int(tid), "Awaiting approval")
        except Exception as e:
            ctx.event("error", f"task {tid}: could not move it to Awaiting approval: {e}")

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
        sel = self.selections.get(ctx.project_id)
        if sel is not None:
            self._report_refused(ctx, [t for t in sel.qualified]
                                 + [s.task for s in sel.skipped])
            if self._merge_granted(ctx, p, sel):
                qualified = [t.as_dict() for t in sel.qualified]
        bench = self._bench(ctx.project_id)
        if bench and sel is not None:
            self._release(ctx, sel, bench)
        merging = self._merging(ctx.project_id)
        if not (bench or merging) or sel is None:
            return qualified
        keep = []
        for t in sel.qualified:
            if str(t.id) in merging:
                sel.skipped.append(tk.Skipped(t, "running", "in the merge queue"))
            elif str(t.id) in bench:
                sel.skipped.append(tk.Skipped(t, "operator", bench[str(t.id)].reason +
                                              "; answer on the task or press Run again to "
                                              "retry it"))
            else:
                keep.append(t)
        sel.qualified[:] = keep
        sel.skipped.sort(key=lambda s: s.task.id)
        return [t.as_dict() for t in keep]

    # -- grants (#979, docs/prd/ap-permission-grants.md) --

    def _ignored(self, ctx, tid, refused):
        """One `autopilot: grant ignored` comment per refused line, never twice."""
        ledger = getattr(self.pool, "ledger", None)
        write = getattr(self.pm, "comment", None)
        if ledger is None or write is None:
            return
        new = ledger.unreported(ctx.project_id, tid, refused)
        for r in new:
            try:
                write(int(tid), f"autopilot: grant ignored: {r['line']} ({r['reason']})")
            except Exception as e:
                ctx.event("error", f"task {tid}: could not comment `grant ignored`: {e}")
                return
            ledger.report(ctx.project_id, tid, [r["key"]])

    def _report_refused(self, ctx, task_list):
        for t in task_list:
            if t.refused:
                self._ignored(ctx, str(t.id), [r.as_dict() for r in t.refused])

    def merge_target(self, project, branch):
        """The worktree a `grant: merge <branch>` merges from, or raise GrantRefused (PRD 5.2):
        not the default branch, and checked out in a worktree under worktree_root."""
        P = wk._permissions()
        P._check_branch_name(branch)
        if branch in P.protected_branches(project.repo):
            raise P.GrantRefused(f"`{branch}` is the default branch")
        held = wk.worktree_holding(project.repo, branch)
        root = wk.worktree_root(project)
        if not held or not P._under(os.path.normpath(held), os.path.normpath(root)) \
                or os.path.normpath(held) == os.path.normpath(root):
            raise P.GrantRefused(f"`{branch}` has no worktree under {root}")
        return held

    def _merge_granted(self, ctx, project, sel):
        """PRD 5.5: an Approved task whose answer holds a valid, unused merge grant starts no
        worker. It moves to In Progress and its branch goes to the merge queue. True when the
        selection changed."""
        pid, ledger = ctx.project_id, getattr(self.pool, "ledger", None)
        if ledger is None:
            return False
        changed = False
        for t in list(sel.qualified):
            if t.status != "Approved":
                continue
            used = ledger.used_keys(pid, str(t.id))
            merges = [g for g in t.grants if g.action == "merge" and g.key not in used]
            if not merges:
                continue
            g = merges[-1]
            try:
                if self.merger is None:
                    raise wk._permissions().GrantRefused("this supervisor has no merge queue")
                wt = self.merge_target(project, g.arg)
            except wk._permissions().GrantRefused as e:
                self._ignored(ctx, str(t.id), [{"key": g.key, "line": g.line, "reason": str(e)}])
                continue
            try:
                self.pm.comment(t.id, f"autopilot: merge granted {g.arg}. The merge queue merges "
                                      "it: the default branch is merged in, the pre-merge gate "
                                      "runs, then a fast-forward. No worker runs.")
                self.pm.transition(t.id, "In Progress")
            except Exception as e:
                ctx.event("error", f"task {t.id}: merge grant not started: {e}")
                continue
            ledger.use(pid, str(t.id), [g])
            ctx.event("merge", f"task {t.id}: merge granted for {g.arg}")
            self.merge(ctx, [{"task": str(t.id), "outcome": "merge", "branch": g.arg,
                              "worktree": wt}])
            sel.qualified.remove(t)
            sel.skipped.append(tk.Skipped(t, "running", "in the merge queue (merge granted)"))
            changed = True
        if changed:
            sel.skipped.sort(key=lambda s: s.task.id)
        return changed

    def start_workers(self, ctx, tasks):
        try:
            started = self.pool.start_workers(ctx, tasks)
        except projects.ConfigError as e:
            # a projects.yaml entry that would widen the template, or no nomad-pm to hand over
            raise PassError(f"worker profile not generated: {e}") from e
        for w in started:
            run = (getattr(self.pool, "runs", None) or {}).get((ctx.project_id, str(w.get("task"))))
            rep = getattr(run, "reported", None) or {}
            if rep.get("grants"):
                ctx.event("grants", f"task {w.get('task')}: " + "; ".join(rep["grants"]))
            if rep.get("grants_refused"):
                self._ignored(ctx, str(w.get("task")), rep["grants_refused"])
        return started

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
        ops, waits = tk.needs_operator(sel), sel.of_kind("waiting")
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
