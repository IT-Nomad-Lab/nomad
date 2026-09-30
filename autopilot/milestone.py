"""The autopilot's milestone gate: runs when the last task of a nomad-pm Feature merges (PRD 5.5,
5.6, 5.7; task #872).

It plugs into the merge queue's `after_merge(ctx, result)` hook, so it runs in the queue's own
thread right after a local `--ff-only` merge, before the next branch is merged:

    q = MergeQueue(...)
    with_milestones(q)                      # q.after_merge = MilestoneGate(... shared with q)

For a result whose `result` is `merged`:

1. Walk the task's parents up to its Feature. No Feature, or the Feature already closed: done.
2. Is this the Feature's last task? Every buildable descendant (Task, Bug, Sub-task, at any depth
   under Epics and Stories) must be closed. The task that just merged counts as closed even if
   the queue's tracker write failed. Otherwise: done, nothing runs.
3. Run the project's `milestone_gate` (default: the pre-merge gate plus `build`, if declared) on
   the default branch's tip, through the same generated wrappers and GateRunner as the merge
   queue, so locks, env, known failures, flakes and the test-count baseline apply the same way.
   Where: the merged task's worktree when its HEAD is the default branch's tip (it is, right after
   the fast-forward, and its build caches are warm); else a detached worktree
   `<worktree_root>/milestone-<project>` at the tip. Nothing is ever reset, stashed or cleaned; a
   milestone worktree with local changes to tracked files is reported, not touched.
4. Green: the Epics and Stories between the task and the Feature whose descendants are all closed
   move to Done, then the Feature moves to Done with the commit and the test table, and a
   `feature_complete` alert goes out (PRD 5.7).
   Red: the Feature moves to Blocked with the failing tests, and a `milestone_failed` alert goes
   out. The Feature stays open: the next task merged under it (a fix) runs the gate again.
5. Push stays the operator's (PRD 4, 5.6): the gate never pushes and never merges the default
   branch anywhere beyond the local fast-forward. When the default branch has a remote to go to
   (no upstream yet, or commits ahead of it), a green milestone surfaces that as one Task in
   Awaiting approval on the project's board, lane `manager` so no worker ever picks it up, plus
   a `push_request` alert. While that request is open, later milestones comment on it instead
   of opening another (its id is kept in milestone-state.json).

Alerts go through `alert(ctx, kind, text)`; the default logs a project event of kind `alert`
(the cockpit's last event) until the alert fan-out (R8) takes them over. Tracker reads and writes
go through `MilestonePM` (the engine's OpenProject client); tests pass a fake. A tracker failure
is logged as a project event and never stops the merge queue.

Run the tests: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest autopilot -q
"""
import dataclasses
import importlib.util
import json
import logging
import os
import sys
import threading
import time

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if NOMAD not in sys.path:
    sys.path.insert(0, NOMAD)

from autopilot import merge as merge_mod  # noqa: E402
from autopilot import permissions  # noqa: E402
from autopilot import projects as projects_mod  # noqa: E402
from autopilot.merge import GateRunner, format_table, git  # noqa: E402
from autopilot.tasks import BUILDABLE, CLOSED, board_for  # noqa: E402
from autopilot.worker import default_branch  # noqa: E402

log = logging.getLogger("autopilot.milestone")

FEATURE = "Feature"
DONE, BLOCKED, OPERATOR = "Done", "Blocked", "Awaiting approval"
PUSH_LANE = "manager"           # a person's lane: task selection never hands it to a worker


# ---- the tracker -------------------------------------------------------------------------------

class MilestonePM:
    """nomad-pm reads and writes the gate needs, on the engine's OpenProject client. Work packages
    come back as {"id", "type", "status", "subject", "parent"}; tests pass a fake with the same
    methods."""

    def __init__(self, op_module=None):
        self._op = op_module
        self._clients = {}

    def _mod(self):
        if self._op is None:
            f = os.path.join(NOMAD, "v2", "pm_openproject.py")
            spec = importlib.util.spec_from_file_location("nomad_pm_openproject", f)
            self._op = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self._op)
        return self._op

    def _c(self, board=None):
        key = board or ""
        if key not in self._clients:
            mod = self._mod()
            self._clients[key] = mod.OpenProject(project=board) if board else mod.OpenProject()
        return self._clients[key]

    def _wp(self, w):
        links = w.get("_links", {})
        return {"id": w["id"], "subject": w.get("subject", ""),
                "type": (links.get("type") or {}).get("title") or "?",
                "status": (links.get("status") or {}).get("title") or "?",
                "parent": self._mod().parent_id(w)}

    def get(self, wp_id):
        return self._wp(self._c().get(int(wp_id)))

    def children(self, wp_id):
        return [self._wp(w) for w in
                self._c()._all([{"parent": {"operator": "=", "values": [str(int(wp_id))]}}])]

    def closed(self, status):
        try:
            return self._c().closed(status)
        except Exception:
            return status in CLOSED

    def move(self, wp_id, status, comment):
        self._c().move(int(wp_id), status, comment)

    def comment(self, wp_id, text):
        self._c().comment(int(wp_id), text)

    def create(self, board, type_name, subject, description, lane):
        return self._c(board).create(type_name, subject, lane=lane, description=description)["id"]


# ---- persisted state ---------------------------------------------------------------------------

class MilestoneState:
    """milestone-state.json next to the merge queue's state: per project, the open push request
    and the last milestone run per Feature. Atomic writes."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"projects": {}}
        try:
            with open(path) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("projects"), dict):
                self.data = d
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.error("milestone state unreadable (%s); starting empty", e)

    def project(self, pid):
        with self.lock:
            p = self.data["projects"].setdefault(pid, {})
            p.setdefault("features", {})
            return p

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)


# ---- git helpers -------------------------------------------------------------------------------

def worktree_of(repo, branch):
    """The path of the worktree that has `branch` checked out, or None."""
    rc, out, _ = git(repo, "worktree", "list", "--porcelain")
    if rc != 0 or not branch:
        return None
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path:
            return path
    return None


def tracked_changes(wt):
    return git(wt, "status", "--porcelain", "--untracked-files=no")[1]


def push_status(repo, default):
    """(needed, why). Needed when the default branch has somewhere to go: a remote exists and
    there is no upstream, or it is ahead of its upstream. Read-only; never fetches."""
    rc, remotes, _ = git(repo, "remote")
    if rc != 0 or not remotes.strip():
        return False, "the repo has no remote; nothing to push"
    rc, up, _ = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{default}@{{upstream}}")
    if rc != 0 or not up:
        return True, (f"`{default}` has no upstream branch (remotes: "
                      f"{', '.join(remotes.split())})")
    rc, n, _ = git(repo, "rev-list", "--count", f"{up}..{default}")
    ahead = int(n) if rc == 0 and n.isdigit() else None
    if ahead == 0:
        return False, f"`{default}` is level with `{up}` as last fetched"
    return True, (f"`{default}` is {ahead if ahead is not None else 'some'} commit(s) ahead of "
                  f"`{up}` as last fetched")


# ---- the gate ----------------------------------------------------------------------------------

def milestone_wrappers(project, state_dir):
    """The project's wrappers (permissions.write_wrappers) plus one for each milestone-gate
    command no wrapper covers yet (a command listed only under `milestone_gate`). Those extra
    wrappers are the supervisor's alone: they are not in any worker's settings."""
    ws = permissions.write_wrappers(project, state_dir)
    ap = project.autopilot
    have = [w.command for w in ws]
    bin_dir = os.path.join(permissions._safe_path("state_dir", state_dir), "bin")
    wt_root = permissions._safe_path("worktree_root", ap.worktree_root)
    for i, c in enumerate(ap.milestone_gate):
        if c in have:
            continue
        name = f"{project.id}-milestone-{i + 1}"
        text, takes = permissions._script(project, name, c, permissions._locks_for(ap, True, c), wt_root)
        w = permissions.Wrapper(name, os.path.join(bin_dir, name), c, takes, text)
        os.makedirs(bin_dir, exist_ok=True)
        with open(w.path + ".tmp", "w") as f:
            f.write(text)
        os.chmod(w.path + ".tmp", 0o555)
        os.replace(w.path + ".tmp", w.path)
        ws.append(w)
        have.append(c)
    return ws


def _default_alert(ctx, kind, text):
    ctx.event("alert", f"{kind}: {text.splitlines()[0][:300] if text else ''}")


class MilestoneGate:
    """after_merge hook for merge.MergeQueue. Callable as gate(ctx, result).

    project_of(pid) -> projects.Project; pm: MilestonePM or fake; runner, gate_secs, state_dir:
    as the merge queue's (with_milestones shares them); baseline_of(pid) -> {suite: count}, the
    last green pre-merge gate's test counts; alert(ctx, kind, text) raises an alert."""

    def __init__(self, project_of=None, pm=None, state_dir=None, runner=None, gate_secs=None,
                 baseline_of=None, alert=None, board_of=None):
        self.project_of = project_of or projects_mod.get
        self.pm = pm if pm is not None else MilestonePM()
        self.state_dir = state_dir or merge_mod.STATE_DIR
        self.state = MilestoneState(os.path.join(self.state_dir, "milestone-state.json"))
        self.runner = runner or merge_mod.run_command
        self.gate_secs = merge_mod.GATE_SECS if gate_secs is None else gate_secs
        self.baseline_of = baseline_of or (lambda pid: {})
        self.alert = alert or _default_alert
        self.board_of = board_of or board_for
        self.results = []           # what each call decided, newest last (the cockpit, tests)

    # -- the hook --

    def __call__(self, ctx, result):
        if (result or {}).get("result") != "merged":
            return None
        try:
            res = self.check(ctx, result)
        except Exception as e:
            log.exception("milestone check after task %s failed", result.get("task"))
            ctx.event("error", f"task {result.get('task')}: milestone gate hit an error: "
                               f"{type(e).__name__}: {e}")
            self.alert(ctx, "milestone_failed",
                       f"milestone check after task {result.get('task')} hit an error: "
                       f"{type(e).__name__}: {e}")
            res = {"result": "error", "task": result.get("task"), "error": str(e)}
        self.results.append(res)
        del self.results[:-50]
        return res

    # -- the tree --

    def _closed(self, wp, merged_id):
        return wp["id"] == merged_id or self.pm.closed(wp["status"])

    def feature_path(self, task_id):
        """(feature, [containers between the task and the feature, nearest first]) or (None, [])."""
        path, seen = [], set()
        wp = self.pm.get(task_id)
        while wp.get("parent") and wp["parent"] not in seen:
            seen.add(wp["parent"])
            wp = self.pm.get(wp["parent"])
            if wp["type"] == FEATURE:
                return wp, path
            path.append(wp)
        return None, []

    def descendants(self, wp_id):
        out, todo, seen = [], [wp_id], {wp_id}
        while todo:
            for c in self.pm.children(todo.pop()):
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                out.append(c)
                todo.append(c["id"])
        return out

    def open_tasks(self, root_id, merged_id):
        return [w for w in self.descendants(root_id)
                if w["type"] in BUILDABLE and not self._closed(w, merged_id)]

    # -- one merged task --

    def check(self, ctx, result):
        pid, tid = ctx.project_id, int(result["task"])
        feature, between = self.feature_path(tid)
        if feature is None:
            return {"result": "no_feature", "task": tid}
        fid = feature["id"]
        if self.pm.closed(feature["status"]):
            return {"result": "feature_closed", "task": tid, "feature": fid}
        left = self.open_tasks(fid, tid)
        if left:
            ctx.event("milestone", f"task {tid}: Feature #{fid} has {len(left)} open task(s) left")
            return {"result": "not_last", "task": tid, "feature": fid,
                    "open": [w["id"] for w in left]}
        ctx.event("milestone", f"task {tid} was the last of Feature #{fid}: milestone gate running")
        return self.run_gate(ctx, result, feature, between)

    def run_gate(self, ctx, result, feature, between):
        pid, tid, fid = ctx.project_id, int(result["task"]), feature["id"]
        project = self.project_of(pid)
        ap = project.autopilot if project else None
        head = f"autopilot: milestone gate for Feature #{fid} ({feature['subject']})"
        if ap is None or not ap.milestone_gate:
            return self._failed(ctx, feature, tid, None,
                                f"{head}: needs operator. Project {pid!r} declares no milestone "
                                "gate, so nothing proves the default branch sound. The Feature "
                                "stays open.", reason="no milestone gate")
        repo = project.repo
        default = default_branch(repo)
        rc, tip, _ = git(repo, "rev-parse", "--verify", f"refs/heads/{default}")
        if rc != 0:
            return self._failed(ctx, feature, tid, None,
                                f"{head}: cannot read `{default}` in {repo}.", reason="no default")
        wt, why = self.place(project, result, default, tip)
        if wt is None:
            return self._failed(ctx, feature, tid, None,
                                f"{head}: needs operator. No clean worktree to run it in: {why} "
                                "Nothing was reset, stashed or cleaned. The Feature stays open.",
                                reason=why)
        ws = milestone_wrappers(project, self.state_dir)
        log_dir = os.path.join(self.state_dir, "milestone", pid,
                               f"{fid}-{time.strftime('%Y%m%d-%H%M%S')}")
        gp = dataclasses.replace(project, autopilot=dataclasses.replace(ap, pre_merge_gate=ap.milestone_gate))
        gate = GateRunner(gp, ws, log_dir, self.runner, self.gate_secs).run(wt, dict(self.baseline_of(pid)))
        with self.state.lock:
            self.state.project(pid)["features"][str(fid)] = {
                "commit": tip, "ok": gate.ok, "at": time.time(), "task": tid}
            self.state.save()
        where = f"on `{default}` at {tip} (in {wt})"
        if not gate.ok:
            failing = "\n".join(f"- {r}" for r in gate.real)
            return self._failed(ctx, feature, tid, gate,
                                f"{head} FAILED {where}. The Feature stays open; the next task "
                                f"merged under it runs the gate again. Nothing was pushed.\n\n"
                                f"Failing:\n{failing}\n\n{format_table(gate)}\n\nGate logs: {log_dir}",
                                reason="gate failed")
        return self._passed(ctx, project, feature, between, tid, gate, default, tip, where, log_dir)

    def place(self, project, result, default, tip):
        """(worktree, "") to run the gate in, or (None, why)."""
        repo = project.repo
        wt = worktree_of(repo, result.get("branch"))
        if wt and os.path.isdir(wt) and git(wt, "rev-parse", "HEAD")[1] == tip \
                and not tracked_changes(wt):
            return wt, ""
        wt = os.path.join(project.autopilot.worktree_root, f"milestone-{project.id}")
        if not os.path.isdir(wt):
            rc, _, err = git(repo, "worktree", "add", "--detach", wt, tip)
            if rc != 0:
                return None, f"`git worktree add --detach {wt}` failed: {err[-300:]}"
            return wt, ""
        if tracked_changes(wt):
            return None, f"the milestone worktree {wt} has local changes to tracked files."
        rc, _, err = git(wt, "checkout", "--detach", tip)
        if rc != 0:
            return None, f"cannot move the milestone worktree {wt} to {tip[:10]}: {err[-300:]}"
        return wt, ""

    # -- outcomes --

    def _move(self, ctx, wp_id, status, comment):
        try:
            self.pm.move(wp_id, status, comment)
            return True
        except Exception as e:
            log.exception("tracker move of %s failed", wp_id)
            ctx.event("error", f"#{wp_id}: tracker move to {status} failed: {type(e).__name__}: {e}")
            return False

    def _failed(self, ctx, feature, tid, gate, comment, reason):
        fid = feature["id"]
        self._move(ctx, fid, BLOCKED, comment)
        self.alert(ctx, "milestone_failed", f"Feature #{fid} {feature['subject']}: {reason}\n{comment}")
        res = {"result": "failed", "task": tid, "feature": fid, "reason": reason, "comment": comment}
        if gate is not None:
            res["failing"] = gate.real
        return res

    def _passed(self, ctx, project, feature, between, tid, gate, default, tip, where, log_dir):
        pid, fid = project.id, feature["id"]
        closed = []
        for c in between:                               # nearest first: Epics before the Feature
            if not self.pm.closed(c["status"]) and not self.open_tasks(c["id"], tid) \
                    and self._move(ctx, c["id"], DONE,
                                   f"autopilot: every task under this {c['type']} is done; "
                                   f"Feature #{fid}'s milestone gate is green on {tip[:10]}."):
                closed.append(c["id"])
        needed, push_why = push_status(project.repo, default)
        lines = [f"autopilot: milestone gate green for Feature #{fid} {where}. Every task under "
                 "it is merged locally. Not pushed.", "", format_table(gate), ""]
        if gate.known:
            lines.append("Known failures (allowed): " + ", ".join(gate.known))
        lines.append("Flakes rerun (passed 3 solo reruns): " + (", ".join(gate.flakes) or "none"))
        lines.append(f"Gate logs: {log_dir}")
        push_id = None
        if needed:
            push_id = self.request_push(ctx, project, feature, default, tip, push_why)
            lines.append(f"Push: {push_why}. Pushing or merging `{default}` beyond this machine "
                         "is the operator's call"
                         + (f": see #{push_id} (Awaiting approval)." if push_id else "."))
        else:
            lines.append(f"Push: not needed ({push_why}).")
        comment = "\n".join(lines)
        ok = self._move(ctx, fid, DONE, comment)
        self.alert(ctx, "feature_complete",
                   f"Feature #{fid} {feature['subject']} complete: milestone gate green on "
                   f"`{default}` {tip[:10]}" + (f"; push awaits approval in #{push_id}" if push_id else ""))
        res = {"result": "closed" if ok else "passed", "task": tid, "feature": fid, "commit": tip,
               "closed": closed + ([fid] if ok else []), "comment": comment}
        if push_id:
            res["push_request"] = push_id
        return res

    def request_push(self, ctx, project, feature, default, tip, why):
        """Surface the push as Awaiting approval: comment on the open request, or open one.
        Never pushes. Returns the request's id, or None if the tracker refused."""
        pid, fid = project.id, feature["id"]
        body = (f"autopilot: Feature #{fid} ({feature['subject']}) passed its milestone gate on "
                f"`{default}` at {tip} in {project.repo}. {why}.\n\n"
                f"Pushing `{default}`, or merging it anywhere beyond the local fast-forward, needs "
                "the operator's OK (PRD 5.6: push is reserved). The autopilot never does it. "
                "Operator: push (or decide not to) yourself, then move this task to Done. "
                "Do not move it to Ready: no worker may push.")
        with self.state.lock:
            p = self.state.project(pid)
            open_id = p.get("push_request")
        if open_id:
            try:
                if not self.pm.closed(self.pm.get(open_id)["status"]):
                    self.pm.comment(open_id, body)
                    self.alert(ctx, "push_request", f"push of `{default}` still awaits approval "
                                                    f"in #{open_id} (now also Feature #{fid})")
                    return open_id
            except Exception as e:
                ctx.event("error", f"push request #{open_id}: {type(e).__name__}: {e}")
        board = self.board_of(pid)
        if not board:
            ctx.event("error", f"project {pid} has no nomad-pm board for the push request")
            self.alert(ctx, "push_request", body)
            return None
        try:
            new_id = self.pm.create(board, "Task",
                                    f"Push `{default}` of {pid}: Feature #{fid} milestone green "
                                    f"({tip[:10]})", body, PUSH_LANE)
        except Exception as e:
            log.exception("push request for %s failed", pid)
            ctx.event("error", f"push request for Feature #{fid}: {type(e).__name__}: {e}")
            self.alert(ctx, "push_request", body)
            return None
        self._move(ctx, new_id, OPERATOR, "autopilot: needs operator: push is reserved (PRD 5.6).")
        with self.state.lock:
            self.state.project(pid)["push_request"] = new_id
            self.state.save()
        self.alert(ctx, "push_request", f"push of `{default}` awaits approval in #{new_id} "
                                        f"(Feature #{fid})")
        return new_id


def with_milestones(queue, pm=None, alert=None, board_of=None):
    """Hook a MilestoneGate into a MergeQueue, sharing its project lookup, runner, gate time
    limit, state folder and test-count baselines. Returns the gate."""
    def baseline_of(pid):
        with queue.state.lock:
            return dict(queue.state.project(pid)["baseline"])
    gate = MilestoneGate(project_of=queue.project_of, pm=pm, state_dir=queue.state_dir,
                         runner=queue.runner, gate_secs=queue.gate_secs, baseline_of=baseline_of,
                         alert=alert, board_of=board_of)
    queue.after_merge = gate
    return gate
