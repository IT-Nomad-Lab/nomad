"""Hybrid execution for the projects that opt in (#1488, feature #1451).

A project whose autopilot block says `execution: {mode: hybrid}` has its tasks run by
agent_execution's ExecutionController instead of autopilot/worker.py. Every other project goes
to the legacy WorkerPool, unchanged, and a hub where no project opts in never builds this pool
(supervisor.build_scheduler).

What changes for an opted-in project:
- The agent (claude, codex or the local model) runs headless and sandboxed in the task worktree:
  no network, no tracker, no git writes. It leaves its change uncommitted.
- The controller commits that change (CandidateFinalizer), merges the default branch in when it
  moved (DefaultBranchIntegration), and runs the project's gates on the exact commit in a
  sandbox (CommitVerifier). Only a verified commit is reported `merge`; the merge queue then
  merges it as for any other run.
- Tracker effects leave through the controller's outbox as comments with a stable effect id,
  so a lost acknowledgement never posts twice. Status moves stay with the scheduler and the
  merge queue.
- Attempts spread across the configured backends, so cloud and local runs go side by side. The
  local backend takes the GPU lease for its whole run, one run at a time.

The attempt store is SQLite (WAL) under STATE_DIR/execution, private to the supervisor;
`python3 -m autopilot.hybrid backup` makes a dated online copy and keeps 14 days;
`python3 -m autopilot.hybrid rollback ...` is the operator's staged rollback (#1496).
"""
import getpass
import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from autopilot import state as state_mod
from autopilot import worker as wk

NOMAD = wk.NOMAD
EXEC_DIR = os.path.join(wk.STATE_DIR, "execution")
POLICY_REVISION = "hybrid-1"
TOOL_PROFILE = "tools:hybrid-native"
COST_POLICY = "budget:plans-and-local"   # subscription plans and local models; no paid API key
TOOLS = ("Read", "Write", "Edit", "Bash", "apply_patch")   # apply_patch: Codex file edits
ACTIONS = ("read", "write", "shell")
LOCAL_ALIAS = "longdoc-local"    # :8240 with no fallback; `code` falls back to codex (cloud)
LITELLM_URL = os.environ.get("NOMAD_HYBRID_LITELLM", "http://127.0.0.1:4000")
LITELLM_CONFIG = os.path.join(NOMAD, "litellm", "config.yaml")
LITELLM_KEY_FILE = os.path.expanduser(os.environ.get("NOMAD_HYBRID_LITELLM_KEY_FILE",
                                                     "~/.config/nomad/litellm-key"))
LOCAL_HEALTH = "http://127.0.0.1:8240/health"
LOCAL_PROPS = "http://127.0.0.1:8240/props"
LOCAL_PROMPT_TOKENS = 36000   # estimate; re-measure with tool search on (#1505)
LOCAL_OUTPUT_TOKENS = 8192
LOCAL_MIN_CTX = LOCAL_PROMPT_TOKENS + LOCAL_OUTPUT_TOKENS
MEDIA_STATE = os.path.expanduser("~/.local/state/content-media-worker/gpu_guard_state.json")
DEPS = Path(os.environ.get("NOMAD_SANDBOX_DEP_PREFIX",
                           os.path.expanduser("~/.local/share/nomad-native-deps")))
COOLDOWN_SECS = 600      # a backend that could not start is skipped this long
PREPARE_SECS = 1800
BACKUP_KEEP_DAYS = 14
MAX_REQUIREMENT_LINES = 200
PRIORITY = {"low": "low", "normal": "normal", "high": "high", "immediate": "urgent",
            "urgent": "urgent"}
EFFECT_LINE = re.compile(r"^effect: (\S+) sha256:([0-9a-f]{64})$", re.M)

INSTRUCTIONS = """\
You are a NOMAD hybrid worker. Work only inside the task worktree, your current directory.
Make the change the task asks for and meet every requirement listed with it.
Do not run `git commit`, `git push` or change branches: leave your changes uncommitted. The
controller commits them and runs the acceptance gates itself, in a sandbox, on that commit.
You have no network and no tracker access; do not try to reach nomad-pm or the internet.
Follow the repository's AGENTS.md and CLAUDE.md. Never edit tests to make them pass.
End with a short summary: what changed, what you checked, what is left.
"""


class HybridError(Exception):
    """A hybrid run that cannot start or be followed; the message says why."""


def is_hybrid(project):
    ap = getattr(project, "autopilot", None)
    ex = getattr(ap, "execution", None) if ap else None
    return bool(ex and ex.hybrid)


def any_hybrid(listing):
    return any(is_hybrid(p) for p in listing)


# ---- the task as the controller sees it ----------------------------------------------------

def task_id(tid):
    return f"openproject:{int(tid)}"


def tid_of(task_ref):
    return str(int(str(task_ref).rsplit(":", 1)[-1]))


def work_item(pm, tid):
    """{subject, description, priority} of nomad-pm task `tid`, read fresh."""
    wp = pm.work_package(int(tid))
    return {"subject": wp.get("subject") or "",
            "description": ((wp.get("description") or {}).get("raw") or "").strip(),
            "priority": (((wp.get("_links") or {}).get("priority") or {}).get("title") or "Normal")}


def revision(item, base, project_id):
    """Changes when the task's text, its base commit or the hybrid policy changes, and not when
    a comment or a status move touches the task: the controller re-checks it at every phase."""
    raw = json.dumps([project_id, item["subject"], item["description"], base, POLICY_REVISION])
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def task_spec(project, item, base, tid):
    from agent_execution.contracts import (AcceptanceCriterion, Budget, ExecutionPolicy, Privacy,
                                           Repository, TaskSpec)
    ex = project.autopilot.execution
    lines = [ln.rstrip() for ln in item["description"].splitlines() if ln.strip()]
    reqs = tuple(lines[:MAX_REQUIREMENT_LINES]) or (item["subject"] or f"task {tid}",)
    if len(lines) > MAX_REQUIREMENT_LINES:
        # Never cut requirements silently: the agent is told the rest exists.
        reqs += (f"(the task description continues for {len(lines) - MAX_REQUIREMENT_LINES} more "
                 "lines in nomad-pm; ask the operator for them before finishing)",)
    return TaskSpec(
        task_id=task_id(tid), task_revision=revision(item, base, project.id), project_id=project.id,
        repository=Repository(identity=project.id, path=os.path.realpath(project.repo), base_commit=base),
        objective=item["subject"] or f"nomad-pm task {tid}", requirements=reqs,
        acceptance_criteria=tuple(AcceptanceCriterion(id=g.id, check=g.run, expected="exit 0")
                                  for g in ex.gates),
        execution_policy=ExecutionPolicy(policy_revision=POLICY_REVISION, privacy=Privacy(ex.privacy),
                                         tool_profile_ref=TOOL_PROFILE, allowed_actions=ACTIONS,
                                         max_attempts=3),
        budget=Budget(wall_seconds=ex.wall_seconds, cost_policy_ref=COST_POLICY),
        priority=PRIORITY.get(item["priority"].lower(), "normal"),
        required_capabilities=("repository_edit",))


# ---- tracker transport ---------------------------------------------------------------------

def effect_comment(effect_id, payload):
    """The comment for one outbox effect. The last line carries the effect id and the payload
    digest; the transport finds an applied effect by it. Never the words `autopilot: result`:
    the legacy tracker reader would take them for a worker's outcome."""
    from agent_execution.delivery import payload_digest
    status, attempt = payload.get("status"), payload.get("attempt_id", "?")
    if status == "Done":
        text = (f"hybrid: candidate verified. Attempt `{attempt}`, commit `{payload.get('commit')}`"
                " passed every gate; the merge queue merges it next.")
    else:
        text = (f"hybrid: attempt `{attempt}` blocked: {payload.get('reason') or 'no reason given'}"
                + (f" ({payload['execution_state']})" if payload.get("execution_state") else "") + ".")
    return f"{text}\n\neffect: {effect_id} sha256:{payload_digest(payload)}"


class PMTransport:
    """TrackerTransport over nomad-pm comments. `client()` is an OpenProject client (v2); the
    task of an effect comes from the store's outbox, since find() gets only the effect id."""

    def __init__(self, client, task_of):
        self.client, self.task_of = client, task_of

    def find(self, effect_id):
        from agent_execution.delivery import AmbiguousResponse, AppliedRecord, TransportUnavailable
        ref = self.task_of(effect_id)
        if ref is None:
            raise AmbiguousResponse("effect is not in the outbox")
        try:
            acts = self.client().activities(int(tid_of(ref)))
        except Exception as e:
            raise TransportUnavailable(str(e)[:200]) from e
        for a in acts:
            for eid, digest in EFFECT_LINE.findall((a.get("comment") or {}).get("raw") or ""):
                if eid == effect_id:
                    return AppliedRecord(eid, digest)
        return None

    def apply(self, effect_id, task_ref, payload):
        from agent_execution.delivery import AmbiguousResponse
        try:
            self.client().comment(int(tid_of(task_ref)), effect_comment(effect_id, payload), as_="dev")
        except Exception as e:
            # A timeout may still have posted it: the next pass looks it up before applying.
            raise AmbiguousResponse(str(e)[:200]) from e


# ---- prepare -------------------------------------------------------------------------------

def prepare(command, workspace, timeout=PREPARE_SECS):
    """The project's install step (`execution.prepare`), run in the worktree before the agent
    starts. The gates run with no network, so dependencies must already be in the worktree.
    It runs the operator's command on the base commit, before any agent edit, with a minimal
    environment; it may write ignored files (.venv, node_modules) and nothing tracked."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "USER") if k in os.environ}
    try:
        p = subprocess.run(["/bin/sh", "-c", command], cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise HybridError(f"prepare took over {timeout}s: {command}") from e
    if p.returncode != 0:
        tail = (p.stderr or p.stdout).strip().splitlines()[-3:]
        raise HybridError(f"prepare failed (exit {p.returncode}): {' / '.join(tail)[:300]}")
    changed = wk.git(workspace, "status", "--porcelain")
    if changed:
        raise HybridError(f"prepare changed tracked files ({changed.splitlines()[0].strip()}); "
                          "nothing was started")


# ---- runs ----------------------------------------------------------------------------------

@dataclass
class HybridRun:
    project: str
    task: str
    attempt_id: str
    backend: str
    branch: str
    worktree: str
    base: str
    started: float
    deadline: float
    record: str
    killed: str = ""
    uncertain: bool = False
    reported: dict = field(default_factory=dict)
    checking: object = None     # the _Check verifying this attempt, while it runs
    cancel_pending: str = ""    # a kill that arrived during verification, applied after it

    def public(self):
        return {"task": self.task, "branch": self.branch, "worktree": self.worktree,
                "log": self.record, "started": self.started, "deadline": self.deadline,
                "backend": self.backend, "attempt_id": self.attempt_id}


class _Check:
    """One verification off the supervisor pass: gates can take 30 minutes and the pass must
    keep polling every other attempt meanwhile. Events are kept and replayed on the pass thread."""

    def __init__(self, fn, background):
        self.out, self.events, self.thread = None, [], None
        if background:
            self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
            self.thread.start()
        else:
            self._run(fn)

    def _run(self, fn):
        try:
            self.out = fn(self)
        except Exception as e:      # _verify reports its own failures; anything else retries
            self.event("error", f"verification crashed: {type(e).__name__}: {e}; next pass")

    def event(self, kind, msg):
        self.events.append((kind, msg))

    def done(self):
        return self.thread is None or not self.thread.is_alive()


class HybridPool:
    """The scheduler's pool: hybrid projects here, the rest to `legacy` (a WorkerPool).

    execution(): the Execution bundle (controller, store, verifier_of, transport, instructions,
    available(backend) -> (ok, why)); built on first use, so a hub that never runs a hybrid
    task never opens the store. fetch(tid) -> work_item dict."""

    def __init__(self, legacy, execution, fetch, project_of=None, clock=time.time,
                 background_checks=True):
        self.legacy = legacy
        self.background_checks = background_checks
        self.project_of = project_of or legacy.project_of
        self._execution, self._ex = execution, None
        self.fetch = fetch
        self.clock = clock
        self.hybrid_runs = {}       # (project, task) -> HybridRun
        self.specs = {}             # task ref -> the TaskSpec the controller checks against
        self.cooling = {}           # backend -> (until, why)

    # the legacy pool's state, shared: the scheduler reads these off the pool
    @property
    def state_dir(self):
        return self.legacy.state_dir

    @property
    def ledger(self):
        return self.legacy.ledger

    @property
    def start_errors(self):
        return self.legacy.start_errors

    @property
    def runs(self):
        return {**self.legacy.runs, **self.hybrid_runs}

    def ex(self):
        if self._ex is None:
            self._ex = self._execution()
        return self._ex

    def _hybrid(self, pid):
        try:
            return is_hybrid(self.project_of(pid))
        except Exception:
            return False

    def _records_dir(self, pid):
        return os.path.join(self.state_dir, "execution", "runs", pid)

    # -- start --

    def pick(self, project, exclude=()):
        """The backend for the next attempt: the configured one with the fewest live attempts,
        in config order on a tie, skipping cooling and unavailable ones. (backend, why-not)"""
        live = {}
        for r in self.hybrid_runs.values():
            live[r.backend] = live.get(r.backend, 0) + 1
        why = []
        now = self.clock()
        order = list(project.autopilot.execution.backends)
        for b in sorted(order, key=lambda b: (live.get(b, 0), order.index(b))):
            if b in exclude:
                continue
            until, reason = self.cooling.get(b, (0, ""))
            if until > now:
                why.append(f"{b} cooling down ({reason})")
                continue
            if b == "local" and live.get("local"):
                why.append("local already runs an attempt (one at a time)")
                continue
            ok, reason = self.ex().available(b)
            if not ok:
                why.append(f"{b}: {reason}")
                continue
            return b, ""
        return None, "; ".join(why) or "no backend configured"

    def launch(self, project, task):
        tid = str(task["task"])
        key = (project.id, tid)
        if key in self.hybrid_runs:
            raise HybridError(f"task {tid} already has a live attempt")
        backend, why = self.pick(project)
        if backend is None:
            raise HybridError(f"no backend can take it: {why}")
        ap = project.autopilot
        wt = wk.ensure_worktree(project.repo, wk.worktree_root(project), tid,
                                task.get("title") or task.get("subject") or "",
                                regenerated=ap.regenerated, noise=ap.noise)
        if wt.dirty:
            raise HybridError(f"{wt.path} has uncommitted changes from an earlier run; the "
                              "operator decides what to keep (nothing was started)")
        workspace = os.path.realpath(wt.path)
        base = wk.git(workspace, "rev-parse", "HEAD")
        if ap.execution.prepare:
            prepare(ap.execution.prepare, workspace)
        spec = task_spec(project, self.fetch(tid), base, tid)
        ex = self.ex()
        self.specs[spec.task_id] = spec      # the policy reads it back during start
        try:
            run = ex.controller.start(spec, backend, ex.instructions, workspace=workspace,
                                      retry_authorized=True)
        except Exception as e:
            self.cooling[backend] = (self.clock() + COOLDOWN_SECS, f"{type(e).__name__}: {e}"[:120])
            raise HybridError(f"{backend} did not start: {type(e).__name__}: {e}") from e
        now = self.clock()
        os.makedirs(self._records_dir(project.id), mode=0o700, exist_ok=True)
        hr = HybridRun(project.id, tid, run.attempt_id, backend, wt.branch, workspace, base, now,
                       now + spec.budget.wall_seconds + 300,
                       os.path.join(self._records_dir(project.id), f"{tid}.json"))
        hr.reported = {"dirty": False, "created": wt.created, "new_branch": wt.new_branch,
                       "grants": [], "grants_refused": []}
        wk._write_json(hr.record, {"project": hr.project, **hr.public(), "base": base})
        self.hybrid_runs[key] = hr
        return hr

    def start_workers(self, ctx, tasks):
        project = self.project_of(ctx.project_id)
        if not is_hybrid(project):
            return self.legacy.start_workers(ctx, tasks)
        started = []
        errors = self.start_errors[ctx.project_id] = []
        for t in tasks:
            try:
                hr = self.launch(project, t)
            except (HybridError, wk.WorkerError, OSError) as e:
                errors.append(f"task {t.get('task')}: {e}")
                ctx.event("error", f"task {t.get('task')}: hybrid attempt not started: {e}")
                continue
            ctx.event("hybrid", f"task {hr.task}: attempt {hr.attempt_id} on {hr.backend}")
            started.append(hr.public())
        return started

    # -- collect --

    def _refresh(self, hr):
        """Re-read the task so the controller checks the attempt against nomad-pm as it is now.
        False when nomad-pm can't be read: the attempt is not polled this pass, because a
        policy refusal would cancel it."""
        try:
            item = self.fetch(hr.task)
        except Exception:
            return False
        self.specs[task_id(hr.task)] = task_spec(self.project_of(hr.project), item, hr.base, hr.task)
        return True

    def _finished(self, hr, outcome, detail, result=None, requeue=False):
        usage = getattr(result, "usage", None)
        cost = getattr(usage, "cost_amount", None)
        warnings = getattr(result, "warnings", ()) or ()
        if warnings:        # e.g. a Codex login refresh that was not written back (#1503)
            detail = f"{detail or ''} Warning: {' '.join(warnings)}".strip()
        out = {"task": hr.task, "outcome": outcome, "cost_usd": cost, "detail": detail,
               "denials": [], "result": "; ".join(result.errors) if result and result.errors else None,
               "branch": hr.branch, "worktree": hr.worktree, "log": hr.record,
               "elapsed_s": round(self.clock() - hr.started, 1), "requeue": requeue}
        state_mod.append_ledger(self.state_dir, {
            "at": self.clock(), "project": hr.project, "task": hr.task, "branch": hr.branch,
            "session_id": hr.attempt_id, "num_turns": None, "total_cost_usd": cost,
            "modelUsage": {}, "outcome": outcome, "detail": (detail or "")[:500],
            "elapsed_s": out["elapsed_s"], "log": hr.record, "backend": hr.backend})
        try:
            with open(hr.record) as f:
                rec = json.load(f)
            wk._write_json(hr.record, {**rec, "collected": outcome, "collected_at": self.clock()})
        except (OSError, ValueError):
            pass
        return out

    def _verify(self, ctx, hr):
        from agent_execution.contracts import ContractError
        from agent_execution.integration import IntegrationConflict
        from agent_execution.verification import VerificationFailed, VerificationUncertain
        ex = self.ex()
        verifier = ex.verifier_of(hr.project)
        try:
            if hr.uncertain:
                # A check claim whose processes were not proven stopped must be reconciled first.
                ex.controller.reconcile_verification(hr.attempt_id, verifier)
                hr.uncertain = False
            ex.controller.verify(hr.attempt_id, verifier)
        except VerificationFailed as e:
            return self._finished(hr, "failed", f"{e}; branch {hr.branch} kept", e.result)
        except VerificationUncertain as e:
            hr.uncertain = True
            ctx.event("hybrid", f"task {hr.task}: verification not settled ({e}); next pass")
            return None
        except IntegrationConflict as e:
            return self._finished(hr, "failed", f"{e}; branch {hr.branch} needs the default branch "
                                  "merged by hand or a new attempt")
        except ContractError as e:
            if "kept moving" in str(e):
                ctx.event("hybrid", f"task {hr.task}: default branch moved during the gates; "
                                    "verifying again next pass")
                return None
            return self._finished(hr, "needs_operator", f"verification refused: {e}; attempt "
                                  f"{hr.attempt_id} stays awaiting verification, {hr.worktree} reserved")
        except Exception as e:
            return self._finished(hr, "failed", f"verification failed: {type(e).__name__}: {e}")
        done = ex.store.result(hr.attempt_id)
        gates = ", ".join(c.gate_id for c in done.test_results) if done.test_results else "the gates"
        return self._verified(hr, done, f"verified on {hr.backend}: commit {done.result_commit} "
                                        f"passed {gates}")

    def _verified(self, hr, result, detail):
        if getattr(result, "result_commit", None) == hr.base:
            # Nothing to finalize: the gates passed on the unchanged base, which proves nothing.
            return self._finished(hr, "failed", f"attempt {hr.attempt_id} on {hr.backend} changed "
                                  "nothing; the gates ran on the unchanged base commit", result)
        return self._finished(hr, "merge", detail, result)

    def _take(self, ctx, hr):
        """The finished check's outcome, its events replayed on the pass thread."""
        check, hr.checking = hr.checking, None
        for kind, msg in check.events:
            ctx.event(kind, msg)
        return check.out

    def _follow(self, ctx, hr):
        """One step for one attempt; a finished dict when it ended, else None."""
        from agent_execution.contracts import AttemptState as S, ContractError
        ex = self.ex()
        if hr.checking is not None:
            # Nothing else touches an attempt while its gates run: no poll, no cancel.
            if not hr.checking.done():
                return None
            return self._take(ctx, hr)  # an unsettled check is tried again next pass, as before
        if hr.cancel_pending:
            reason, hr.cancel_pending = hr.cancel_pending, ""
            try:
                ex.controller.cancel(hr.attempt_id, reason)
            except Exception as e:
                ctx.event("error", f"task {hr.task}: cancel after verification failed: {e}")
        if hr.killed == "" and self.clock() >= hr.deadline:
            hr.killed = "timeout"
            try:
                ex.controller.cancel(hr.attempt_id, "time limit reached")
            except Exception as e:
                ctx.event("error", f"task {hr.task}: cancel at the time limit failed: {e}")
        if not self._refresh(hr):
            ctx.event("hybrid", f"task {hr.task}: nomad-pm unreadable; attempt not polled this pass")
            return None
        try:
            result = ex.controller.poll(hr.attempt_id)
        except ContractError as e:
            return self._finished(hr, "needs_operator", f"attempt {hr.attempt_id}: {e}; the "
                                  f"worktree {hr.worktree} stays reserved until it is reconciled")
        st = ex.store.get(hr.attempt_id).state
        if st == S.VERIFYING:
            hr.checking = _Check(lambda sink: self._verify(sink, hr), self.background_checks)
            return self._take(ctx, hr) if hr.checking.done() else None
        if st == S.SUCCEEDED:
            return self._verified(hr, result, f"verified on {hr.backend}: commit "
                                            f"{getattr(result, 'result_commit', None)}")
        if st == S.CANCELLED:
            if hr.killed:
                why = {"timeout": "time limit reached", "operator": "stopped by the operator"}[hr.killed]
                if wk.git(hr.worktree, "status", "--porcelain"):
                    # The agent's uncommitted work is still there; a new attempt would refuse it.
                    return self._finished(hr, "needs_operator", f"{why}; {hr.worktree} holds the "
                                          "attempt's uncommitted work: keep or discard it", result)
                return self._finished(hr, "resume", f"{why}; branch {hr.branch} kept", result,
                                      requeue=True)
            return self._finished(hr, "failed", "attempt cancelled: the task or its policy changed "
                                  "while it ran", result)
        if st in (S.BLOCKED, S.HANDOFF_READY):
            errors = "; ".join(result.errors) if result and result.errors else "no detail"
            return self._finished(hr, "failed", f"attempt {hr.attempt_id} on {hr.backend} "
                                  f"{st.value}: {errors}", result)
        return None

    def collect(self, ctx, now=None):
        if not self._hybrid(ctx.project_id):
            return self.legacy.collect(ctx, now) if now is not None else self.legacy.collect(ctx)
        done = []
        for key, hr in list(self.hybrid_runs.items()):
            if hr.project != ctx.project_id:
                continue
            out = self._follow(ctx, hr)
            if out is not None:
                done.append(out)
                del self.hybrid_runs[key]
        if self._ex is not None:
            try:
                for d in self._ex.controller.deliver_tracker(self._ex.transport):
                    if d.status not in ("delivered", "reconciled"):
                        ctx.event("hybrid", f"tracker effect {d.effect_id}: {d.status} {d.detail}")
            except Exception as e:
                ctx.event("error", f"tracker delivery: {type(e).__name__}: {e}")
        return done

    # -- recovery and stop --

    def recover(self, ctx, stored=()):
        pid = ctx.project_id
        if not self._hybrid(pid):
            return self.legacy.recover(ctx, stored)
        workers = []
        for f in sorted(glob.glob(os.path.join(self._records_dir(pid), "*.json"))):
            try:
                with open(f) as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            if rec.get("collected"):
                continue
            key = (pid, str(rec["task"]))
            if key in self.hybrid_runs:
                workers.append(self.hybrid_runs[key].public())
                continue
            hr = HybridRun(pid, str(rec["task"]), rec["attempt_id"], rec["backend"], rec["branch"],
                           rec["worktree"], rec["base"], rec["started"], rec["deadline"], f)
            if not self._refresh(hr):
                ctx.event("recover", f"task {hr.task}: nomad-pm unreadable; attempt "
                                     f"{hr.attempt_id} recovered on a later pass")
                continue
            try:
                self.ex().controller.recover(self.specs[task_id(hr.task)], hr.backend, hr.attempt_id)
            except Exception as e:
                ctx.event("recover", f"task {hr.task}: attempt {hr.attempt_id} not adopted: {e}; "
                                     f"{hr.worktree} stays reserved for the operator")
                continue
            self.hybrid_runs[key] = hr
            workers.append(hr.public())
            ctx.event("recover", f"task {hr.task}: hybrid attempt {hr.attempt_id} adopted")
        return workers

    def stop(self, ctx, mode):
        if not self._hybrid(ctx.project_id):
            return self.legacy.stop(ctx, mode)
        if mode != "kill":
            return
        for hr in self.hybrid_runs.values():
            if hr.project == ctx.project_id and not hr.killed:
                hr.killed = "operator"
                if hr.checking is not None:
                    # The gates finish first; an attempt left unverified is cancelled after.
                    hr.cancel_pending = "stopped by the operator"
                    ctx.event("hybrid", f"task {hr.task}: verifying; stops once the gates end")
                    continue
                try:
                    self.ex().controller.cancel(hr.attempt_id, "stopped by the operator")
                except Exception as e:
                    ctx.event("error", f"task {hr.task}: cancel failed: {e}")


# ---- the real composition ------------------------------------------------------------------

@dataclass
class Execution:
    controller: object
    store: object
    verifier_of: object     # project id -> CommitVerifier
    transport: object
    instructions: tuple
    available: object       # backend -> (ok, why)
    pool_ref: dict = field(default_factory=dict)


def litellm_key():
    try:
        with open(LITELLM_KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


LOCAL_HOSTS = ("127.0.0.1", "localhost", "host.docker.internal")
FALLBACK_KEYS = ("fallbacks", "default_fallbacks", "context_window_fallbacks",
                 "content_policy_fallbacks")


def local_only_route(alias, config=None):
    """True when LiteLLM serves `alias` from a local server only: every deployment of it points
    at a local host, and no fallback chain starts from it (`code`, for one, falls back to codex,
    a cloud model). Any doubt reads as False."""
    try:
        import yaml
        with open(config or LITELLM_CONFIG) as f:
            doc = yaml.safe_load(f) or {}
    except Exception:
        return False
    from urllib.parse import urlparse
    entries = [m for m in doc.get("model_list") or [] if m.get("model_name") == alias]
    if not entries:
        return False
    for m in entries:
        base = (m.get("litellm_params") or {}).get("api_base") or ""
        if urlparse(base).hostname not in LOCAL_HOSTS:
            return False
    for section in ("router_settings", "litellm_settings"):
        settings = doc.get(section) or {}
        for key in FALLBACK_KEYS:
            chains = settings.get(key)
            if isinstance(chains, list) and any(
                    not isinstance(c, dict) or alias in c for c in chains):
                # A bare list (default_fallbacks) applies to every alias.
                return False
    return True


def local_up(url=LOCAL_HEALTH):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def local_ctx(url=LOCAL_PROPS):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            props = json.load(r)
        ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
        if ctx is None:
            ctx = props.get("n_ctx")
        return ctx if type(ctx) is int and ctx > 0 else None
    except Exception:
        return None


def private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return Path(path).resolve()


def task_scope(task, run):
    """(branch, allowed paths) for the finalizer. A hybrid task may change any path of its
    repository; the finalizer itself refuses protected and secret paths. Allowed paths are
    exact names or directory prefixes, not globs, so the whole repository is every top-level
    entry of HEAD plus the top-level names of new files."""
    ws = run.workspace
    branch = wk.git(ws, "branch", "--show-current")
    if not branch.startswith(("feat/", "fix/")):
        raise HybridError(f"{ws} is not on a task branch")
    # -z: unquoted names. A failed git gives no names, and the finalizer then refuses.
    names = (wk.git(ws, "ls-tree", "-z", "--name-only", "HEAD")
             + "\0" + wk.git(ws, "ls-files", "-z", "--others", "--exclude-standard")
             + "\0" + wk.git(ws, "diff", "-z", "--name-only", "HEAD")).split("\0")
    return branch, tuple(sorted({n.split("/", 1)[0] for n in names if n}))


def codex_factory(runtime=None):
    """Codex launches through a verified, trusted per-attempt CODEX_HOME (#1486); its policy
    revision, tool profile and socket runtime dir must be the controller's."""
    from agent_execution.codex import codex_adapter_factory
    return codex_adapter_factory(policy_revision=POLICY_REVISION, tool_profile_ref=TOOL_PROFILE,
                                 runtime=runtime)


def build_execution(projects_of, pm, root=EXEC_DIR, adapters_extra=None, runtime=None):
    """Compose the controller for every hybrid project of the hub. Called once, lazily."""
    import shutil
    from agent_execution.context import ArtifactStore
    from agent_execution.contracts import AgentSpec, Boundary, Privacy
    from agent_execution.controller import ExecutionController
    from agent_execution.enforcement import ClaudeEnforcement
    from agent_execution.finalization import CandidateFinalizer
    from agent_execution.integration import DefaultBranchIntegration
    from agent_execution.native import NativeAdapter, NativeProfile
    from agent_execution.policy import EffectivePolicy, ToolProfile
    from agent_execution.resources import local_gpu_admission
    from agent_execution.store import AttemptStore
    from agent_execution.contracts import ContractError
    from agent_execution.tool_authority import AuthorityServer, ToolAuthority, authority_socket_path
    from agent_execution.verification import CommitVerifier, Gate
    root = private_dir(root)
    hybrid = {p.id: p for p in projects_of() if is_hybrid(p)}
    if not hybrid:
        raise HybridError("no project in projects.yaml opts in to hybrid execution")
    try:
        # Every attempt binds a socket under the runtime dir; check one now, not at every launch.
        sockets = authority_socket_path(root, "0" * 36, runtime).parent
    except ContractError as e:
        raise HybridError(f"no tool-authority socket path for {root}: {e}") from e
    store = AttemptStore(root / "attempts.db")
    artifacts = ArtifactStore(private_dir(root / "artifacts"))
    # Keyed like Task.repository.path, so master and main projects share one controller.
    default = {os.path.realpath(p.repo): wk.default_branch(p.repo) for p in hybrid.values()}
    backends = sorted({b for p in hybrid.values() for b in p.autopilot.execution.backends})
    walls = max(p.autopilot.execution.wall_seconds for p in hybrid.values())
    evidence = artifacts.put(json.dumps({
        "claude": "#1461 native enforcement canary, installed CLI",
        "local": "#1461 canary; LiteLLM alias longdoc-local, no fallback (litellm/config.yaml)",
        "codex": "#1486 trusted per-attempt CODEX_HOME, installed CLI probe"}, sort_keys=True).encode())
    local_route = local_only_route(LOCAL_ALIAS)

    def agent(name):
        local = name == "local"
        return AgentSpec(agent_id=name, agent_type="coding_agent",
                         provider={"claude": "anthropic", "codex": "openai", "local": "litellm"}[name],
                         model_role=LOCAL_ALIAS if local else "default",
                         boundary=Boundary.LOCAL if local else Boundary.CLOUD,
                         execution_mode="headless", availability="available",
                         capabilities=("repository_edit",), validated_capabilities=("repository_edit",),
                         validation_ref=evidence, supported_tools=TOOLS,
                         privacy_compatibility=(Privacy.LOCAL_ONLY, Privacy.CLOUD_ALLOWED) if local
                         else (Privacy.CLOUD_ALLOWED,))

    profile = ToolProfile(ref=TOOL_PROFILE, policy_revision=POLICY_REVISION, agent_ids=tuple(backends),
                          tools=TOOLS, allowed_actions=ACTIONS, approval_actions=(),
                          max_wall_seconds=walls, cost_policy_refs=(COST_POLICY,),
                          local_route_verified=local_route)
    pool_ref = {}
    policy = EffectivePolicy(current_task=lambda ref: pool_ref["pool"].specs.get(ref),
                             profiles=(profile,), operators=("operator",), store=store)
    socket_for = lambda run: authority_socket_path(root, run.attempt_id, runtime)
    private_dir(sockets.parent)
    private_dir(sockets)
    enforcement = ClaudeEnforcement(private_dir(root / "permissions"), socket_for)
    env = {"PATH": str(DEPS / "usr/bin") + ":" + os.environ.get("PATH", ""),
           "LD_LIBRARY_PATH": str(DEPS / "usr/lib/x86_64-linux-gnu"),
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    adapters = {}
    if "claude" in backends:
        adapters["claude"] = NativeAdapter(NativeProfile(
            backend="claude", agent=agent("claude"), command_prefix=(claude,), settings_args=(),
            policy_revision=POLICY_REVISION, tool_profile_ref=TOOL_PROFILE, environment=env),
            artifacts, store, private_dir(root / "native"), enforcement=enforcement)
    if "local" in backends:
        model = {k: LOCAL_ALIAS for k in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                                          "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                                          "ANTHROPIC_DEFAULT_HAIKU_MODEL")}
        adapters["local"] = NativeAdapter(NativeProfile(
            backend="local", agent=agent("local"), command_prefix=(claude,), settings_args=(),
            policy_revision=POLICY_REVISION, tool_profile_ref=TOOL_PROFILE,
            environment={**env, **model, "ENABLE_TOOL_SEARCH": "true",
                         "ANTHROPIC_BASE_URL": LITELLM_URL,
                         "ANTHROPIC_AUTH_TOKEN": litellm_key(), "ANTHROPIC_API_KEY": ""}),
            artifacts, store, private_dir(root / "native"), enforcement=enforcement)
    extra = dict(adapters_extra or {})
    missing = {}
    if "codex" in backends and "codex" not in extra:
        try:
            extra["codex"] = codex_factory(runtime)
        except Exception as e:      # no installed CLI: codex stays unavailable, the rest runs
            missing["codex"] = f"{type(e).__name__}: {e}"
    for name, make in extra.items():
        if name in backends:
            try:
                adapters[name] = make(agent(name), artifacts, store, root)
            except Exception as e:
                missing[name] = f"{type(e).__name__}: {e}"

    author = ("NOMAD hybrid controller", "nomad-hybrid@localhost")
    controller = ExecutionController(
        store, artifacts, adapters, authorize=policy,
        resource_for=local_gpu_admission(media_state_path=MEDIA_STATE),
        tool_authority_for=lambda task, agent_, run: AuthorityServer(
            socket_for(run), ToolAuthority(policy, task, agent_, run, routes=(), sandbox_verified=True)),
        finalizer=CandidateFinalizer(artifacts, author), finalization_scope=task_scope,
        integration=DefaultBranchIntegration(author, branch=default))

    # CommitVerifier wants its gates to be exactly the task's criteria: one per project.
    verifiers = {pid: CommitVerifier(artifacts, private_dir(root / "checks" / pid), [
        Gate(g.id, ("/bin/sh", "-c", g.run), g.timeout, g.read_roots)
        for g in p.autopilot.execution.gates]) for pid, p in hybrid.items()}

    def task_of(effect_id):
        for e in store.pending_effects():
            if e["effect_id"] == effect_id:
                return e["task_id"]
        return None

    def available(name):
        if name not in adapters:
            return False, f"adapter not built: {missing.get(name, 'not configured')}"
        if name == "local":
            if not local_route:
                return False, f"LiteLLM gives {LOCAL_ALIAS} a fallback chain; not a local-only route"
            if not litellm_key():
                return False, f"no LiteLLM key in {LITELLM_KEY_FILE}"
            if not local_up():
                return False, ":8240 is down (start it with llama/qwen38.sh start)"
            ctx = local_ctx()
            if ctx is not None and ctx < LOCAL_MIN_CTX:
                return False, (f":8240 n_ctx={ctx} is below prompt estimate {LOCAL_PROMPT_TOKENS} "
                               f"+ output allowance {LOCAL_OUTPUT_TOKENS}; run llama/qwen38.sh stop "
                               "then QWEN38_CTX=65536 llama/qwen38.sh start")
            if ctx is None:
                return True, ":8240 context unreadable; prompt fit unverified"
        return True, ""

    instructions = (artifacts.put(INSTRUCTIONS.encode()),)
    ex = Execution(controller, store, verifiers.__getitem__, PMTransport(lambda: pm._any(), task_of),
                   instructions, available)
    ex.pool_ref = pool_ref
    return ex


def build_pool(legacy, projects_of, pm):
    """The HybridPool with the real composition. `projects_of()` lists the hub's projects;
    `pm` is the scheduler's PMClient."""
    holder = {}

    def make():
        ex = build_execution(projects_of, pm)
        ex.pool_ref["pool"] = holder["pool"]
        return ex

    pool = HybridPool(legacy, make, lambda tid: work_item(pm, tid))
    holder["pool"] = pool
    return pool


# ---- backup --------------------------------------------------------------------------------

def backup(db=None, dest=None, keep_days=BACKUP_KEEP_DAYS, now=None):
    """An online copy of the attempt store (SQLite backup API, safe while the supervisor
    writes), then remove this job's own copies older than keep_days. Returns the new path."""
    db = Path(db or os.path.join(EXEC_DIR, "attempts.db"))
    dest = private_dir(dest or os.path.join(EXEC_DIR, "backups"))
    now = time.time() if now is None else now
    if not db.is_file():
        raise HybridError(f"no attempt store at {db}")
    out = dest / f"attempts-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(out, 0o600)
    for old in dest.glob("attempts-*.db"):
        if old != out and re.fullmatch(r"attempts-\d{8}-\d{6}\.db", old.name) \
                and old.stat().st_mtime < now - keep_days * 86400:
            old.unlink()
    return out


# ---- staged rollback (#1496) ---------------------------------------------------------------

ROLLBACK_USAGE = """usage: python3 -m autopilot.hybrid rollback COMMAND
  status                      what still holds a workspace; changes nothing
  begin --reason R            stop new hybrid dispatch (durable), then status
  handback                    check legacy may own the workspaces again
  resume --reason R           re-enable hybrid dispatch
  schema VERSION --reason R   lower the store's schema for an older supervisor
  forward                     reopen with this code: migrates a rolled-back schema forward"""


def _store_version(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def _rollback_status(found, inv):
    lines = [f"dispatch: {'disabled' if inv['dispatch_disabled'] else 'enabled'}"]
    lines += [f"attempt {i['attempt_id']} (task {i['task_id']}): {i['state']}" for i in inv["unterminated"]]
    lines += [f"reserved: {r['workspace']} by {r['attempt_id']}" for r in inv["reservations"]]
    lines.append(f"pending tracker effects: {len(inv['pending_effects'])} (kept, delivered later)")
    lines += [f"blocker: {b}" for b in found] or ["no blockers: handback and schema rollback may go ahead"]
    return lines


def rollback(command, *, reason="", version=None, db=None, gpu_paths=None, actor=None):
    """One staged-rollback step on the live attempt store; returns the lines to print. It never
    deletes attempts, worktrees or effects (agent_execution/rollback.py). Opening the store with
    this code migrates it forward, so after a schema rollback only status and forward run, and
    status then reads the version without opening it."""
    from agent_execution.contracts import ContractError
    from agent_execution.rollback import RollbackCoordinator
    from agent_execution.store import AttemptStore, SCHEMA_VERSION
    db = Path(db or os.path.join(EXEC_DIR, "attempts.db"))
    actor = actor or f"operator:{getpass.getuser()}"
    gpu_paths = (MEDIA_STATE,) if gpu_paths is None else gpu_paths
    if not db.is_file():
        raise HybridError(f"no attempt store at {db}: no project has run a hybrid task")
    if command in ("begin", "resume", "schema") and not reason.strip():
        raise HybridError(f"rollback {command} needs --reason")
    current = _store_version(db)
    if current < SCHEMA_VERSION and command != "forward":
        if command == "status":
            return [f"schema rolled back to v{current} (this code writes v{SCHEMA_VERSION}); "
                    "the store was not opened. `rollback forward` migrates it back."]
        raise HybridError(f"schema is at v{current}: opening it with this code would migrate it "
                          f"forward; run `rollback forward` first if that is what you want")
    store = AttemptStore(db)
    try:
        rc = RollbackCoordinator(store, gpu_restore_paths=gpu_paths)
        if command == "forward":
            return [f"schema v{_store_version(db)} (was v{current})"]
        if command == "status":
            return _rollback_status(*rc.blockers())
        if command == "begin":
            rc.begin(actor=actor, reason=reason)
            return ["new hybrid dispatch disabled"] + _rollback_status(*rc.blockers())
        if command == "resume":
            rc.resume(actor=actor, reason=reason)
            return ["hybrid dispatch re-enabled"]
        try:
            if command == "handback":
                out = rc.handback()
                return [f"handback allowed: no hybrid writer can act; {len(out['pending_effects'])} "
                        "pending tracker effects kept. Remove `execution:` from the project in "
                        "projects.yaml and redeploy the supervisor to hand the board back."]
            if command == "schema":
                v = rc.schema_rollback(version, actor=actor, reason=reason)
                return [f"schema at v{v}. Deploy the older supervisor now: this one would migrate "
                        "the store forward when it reopens it."]
        except ContractError as e:
            raise HybridError(str(e)) from e
        raise HybridError(f"unknown rollback command {command!r}")
    finally:
        store.close()


def _rollback_main(args):
    reason, version, rest = "", None, []
    it = iter(args)
    for a in it:
        if a == "--reason":
            reason = next(it, "")
        else:
            rest.append(a)
    if not rest or rest[0] not in ("status", "begin", "handback", "resume", "schema", "forward") \
            or len(rest) != (2 if rest[0] == "schema" else 1):
        print(ROLLBACK_USAGE, file=sys.stderr)
        return 2
    if rest[0] == "schema":
        if not rest[1].isdigit():
            print(ROLLBACK_USAGE, file=sys.stderr)
            return 2
        version = int(rest[1])
    try:
        for line in rollback(rest[0], reason=reason, version=version):
            print(line)
    except HybridError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv):
    if argv[1:2] == ["rollback"]:
        return _rollback_main(argv[2:])
    if argv[1:] == ["backup"]:
        if not os.path.isfile(os.path.join(EXEC_DIR, "attempts.db")):
            print("no attempt store yet: no project has run a hybrid task")
            return 0
        print(backup())
        return 0
    print("usage: python3 -m autopilot.hybrid backup | rollback COMMAND", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
