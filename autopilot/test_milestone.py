"""Tests for the milestone gate (#872): the merge queue's real git and fake gate scripts (from
test_merge), a fake nomad-pm tree, and alerts captured in a list.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest autopilot -q
"""
import dataclasses
import os
import shutil
import subprocess
import sys

import pytest

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, NOMAD)

from autopilot import merge as merge_mod  # noqa: E402
from autopilot import milestone as ms  # noqa: E402
from autopilot.projects import Command  # noqa: E402
from autopilot.tasks import CLOSED  # noqa: E402
from autopilot.test_merge import Ctx, Env, sh  # noqa: E402


class FakePM:
    """A nomad-pm board in memory: Feature 100 > Epic 110 > Tasks 1, 2; Task 3 has no Feature."""

    def __init__(self):
        self.wps = {}
        self.moves = []
        self.comments = []
        self.created = []
        self.add(100, "Feature", "In Progress", None, "Milestone one")
        self.add(110, "Epic", "In Progress", 100, "Epic A")
        self.add(1, "Task", "In Progress", 110, "task one")
        self.add(2, "Task", "Done", 110, "task two")
        self.add(3, "Task", "In Progress", None, "loose task")

    def add(self, i, t, status, parent, subject=""):
        self.wps[i] = {"id": i, "type": t, "status": status, "parent": parent, "subject": subject}

    def get(self, i):
        return dict(self.wps[int(i)])

    def children(self, i):
        return [dict(w) for w in self.wps.values() if w["parent"] == int(i)]

    def closed(self, status):
        return status in CLOSED

    def move(self, i, status, comment):
        self.moves.append((int(i), status, comment))
        self.wps[int(i)]["status"] = status

    def comment(self, i, text):
        self.comments.append((int(i), text))

    def create(self, board, type_name, subject, description, lane):
        i = 900 + len(self.created)
        self.created.append({"id": i, "board": board, "type": type_name, "subject": subject,
                             "description": description, "lane": lane})
        self.add(i, type_name, "New", None, subject)
        return i

    def status(self, i):
        return self.wps[i]["status"]


class Rig:
    def __init__(self, env):
        self.env = env
        self.pm = FakePM()
        self.alerts = []

    def queue(self, project):
        q = self.env.queue(project)
        self.gate = ms.with_milestones(q, pm=self.pm, board_of=lambda pid: "board-p",
                                       alert=lambda ctx, kind, text: self.alerts.append((kind, text)))
        return q

    def kinds(self):
        return [k for k, _ in self.alerts]


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def rig(env):
    return Rig(env)


def with_milestone(project, *extra):
    ap = project.autopilot
    return dataclasses.replace(project, autopilot=dataclasses.replace(
        ap, milestone_gate=ap.milestone_gate + tuple(extra)))


def calls_after(env, n):
    return env.calls_list()[n:]


# ---- the last task of a Feature -----------------------------------------------------------------

def test_last_task_runs_the_milestone_gate_and_closes_epic_and_feature(env, rig):
    project = env.project()
    q = rig.queue(project)
    res = q.merge(Ctx(), [env.task("1")])
    assert res[0]["result"] == "merged"
    out = rig.gate.results[-1]
    assert out["result"] == "closed" and out["feature"] == 100
    assert out["commit"] == env.head()
    assert out["closed"] == [110, 100]                       # the Epic first, then the Feature
    assert rig.pm.status(110) == "Done" and rig.pm.status(100) == "Done"
    fcomment = [c for i, s, c in rig.pm.moves if i == 100][0]
    assert "milestone gate green" in fcomment and "Not pushed" in fcomment
    assert "| unit |" in fcomment and "Push: not needed" in fcomment
    assert rig.kinds() == ["feature_complete"]
    assert not rig.pm.created                                # no remote: nothing to push
    # the gate ran on the merged tip, in the task's own worktree
    wt = str(env.wt_root / "ap-1")
    gate_calls = [c for c in env.calls_list() if c["cmd"] in ("step", "test")]
    assert len(gate_calls) == 4                              # pre-merge gate, then milestone gate
    assert gate_calls[-1]["cwd"] == wt
    assert res[0]["cleanup"] == "removed"                    # #1011: only after the milestone gate


def test_a_task_that_is_not_the_last_runs_nothing(env, rig):
    rig.pm.wps[2]["status"] = "Ready"
    q = rig.queue(env.project())
    q.merge(Ctx(), [env.task("1")])
    out = rig.gate.results[-1]
    assert out == {"result": "not_last", "task": 1, "feature": 100, "open": [2]}
    assert not rig.pm.moves and not rig.alerts
    assert len([c for c in env.calls_list() if c["cmd"] == "step"]) == 1   # pre-merge gate only


def test_open_task_deeper_down_also_counts(env, rig):
    rig.pm.add(120, "Epic", "In Progress", 100)
    rig.pm.add(121, "Story", "New", 120)
    rig.pm.add(4, "Bug", "Blocked", 121)
    q = rig.queue(env.project())
    q.merge(Ctx(), [env.task("1")])
    assert rig.gate.results[-1]["open"] == [4]


def test_a_task_without_a_feature_is_ignored(env, rig):
    q = rig.queue(env.project())
    q.merge(Ctx(), [env.task("3")])
    assert rig.gate.results[-1] == {"result": "no_feature", "task": 3}
    assert not rig.pm.moves


def test_a_closed_feature_is_left_alone(env, rig):
    rig.pm.wps[100]["status"] = "Done"
    q = rig.queue(env.project())
    q.merge(Ctx(), [env.task("1")])
    assert rig.gate.results[-1]["result"] == "feature_closed"
    assert not rig.pm.moves


def test_non_merge_results_are_ignored(rig, env):
    g = ms.MilestoneGate(pm=rig.pm, state_dir=str(env.state_dir))
    assert g(Ctx(), {"task": "1", "result": "resume"}) is None
    assert g.results == []


# ---- a red milestone gate -------------------------------------------------------------------------

def test_failing_milestone_gate_blocks_the_feature_and_alerts(env, rig):
    suite = Command(f"python3 {env.bin}/fake_step.py suite")      # only in the milestone gate
    project = with_milestone(env.project(), suite)
    env.config(tests={"T.A1_ok": "pass"}, suite_exit=3)
    q = rig.queue(project)
    res = q.merge(Ctx(), [env.task("1")])
    assert res[0]["result"] == "merged"                       # the task itself is merged
    out = rig.gate.results[-1]
    assert out["result"] == "failed" and out["feature"] == 100
    assert rig.pm.status(100) == "Blocked"
    assert rig.pm.status(110) == "In Progress"                # epics stay open on red
    assert any("exited 3" in f for f in out["failing"])
    assert rig.kinds() == ["milestone_failed"]
    # the milestone-only command ran through a generated wrapper the workers don't have
    assert os.path.exists(env.state_dir / "bin" / "p-milestone-3")
    assert [c for c in env.calls_list() if c["cmd"] == "suite"][0]["cwd"] == str(env.wt_root / "ap-1")


def test_a_fix_merged_under_a_blocked_feature_runs_the_gate_again(env, rig):
    suite = Command(f"python3 {env.bin}/fake_step.py suite")
    project = with_milestone(env.project(), suite)
    env.config(tests={"T.A1_ok": "pass"}, suite_exit=3)
    q = rig.queue(project)
    q.merge(Ctx(), [env.task("1")])
    assert rig.pm.status(100) == "Blocked"
    rig.pm.wps[1]["status"] = "Done"
    rig.pm.add(5, "Task", "In Progress", 100, "fix")
    env.config(tests={"T.A1_ok": "pass"}, suite_exit=0)
    q.merge(Ctx(), [env.task("5", content="fixed\n", fname="b.txt")])
    assert rig.gate.results[-1]["result"] == "closed"
    assert rig.pm.status(100) == "Done"


def test_no_milestone_gate_needs_the_operator(env, rig):
    project = env.project()
    project = dataclasses.replace(project, autopilot=dataclasses.replace(
        project.autopilot, milestone_gate=()))
    q = rig.queue(project)
    q.merge(Ctx(), [env.task("1")])
    out = rig.gate.results[-1]
    assert out["result"] == "failed" and out["reason"] == "no milestone gate"
    assert rig.pm.status(100) == "Blocked" and rig.kinds() == ["milestone_failed"]


# ---- where it runs ------------------------------------------------------------------------------

def test_without_the_task_worktree_it_uses_a_detached_milestone_worktree(env, rig):
    project = env.project()
    rig.pm.wps[1]["status"] = "Done"
    g = ms.MilestoneGate(project_of=lambda pid: project, pm=rig.pm, state_dir=str(env.state_dir),
                         alert=lambda c, k, t: rig.alerts.append((k, t)), board_of=lambda p: "b")
    out = g(Ctx(), {"task": "1", "result": "merged", "branch": "feat/gone"})
    wt = env.wt_root / "milestone-p"
    assert out["result"] == "closed"
    assert sh(wt, "rev-parse", "HEAD") == env.head()
    assert env.calls_list()[-1]["cwd"] == str(wt)
    # the next milestone reuses it, moved to the new tip; the main checkout is untouched
    (env.repo / "c.txt").write_text("c\n")
    sh(env.repo, "add", "c.txt")
    sh(env.repo, "commit", "-qm", "c")
    rig.pm.wps[100]["status"] = "In Progress"
    g(Ctx(), {"task": "1", "result": "merged", "branch": "feat/gone"})
    assert sh(wt, "rev-parse", "HEAD") == env.head()


def test_a_dirty_milestone_worktree_is_reported_not_touched(env, rig):
    project = env.project()
    rig.pm.wps[1]["status"] = "Done"
    wt = env.wt_root / "milestone-p"
    sh(env.repo, "worktree", "add", "-q", "--detach", str(wt), "master")
    (wt / "a.txt").write_text("operator edit\n")
    g = ms.MilestoneGate(project_of=lambda pid: project, pm=rig.pm, state_dir=str(env.state_dir),
                         alert=lambda c, k, t: rig.alerts.append((k, t)))
    out = g(Ctx(), {"task": "1", "result": "merged", "branch": "feat/gone"})
    assert out["result"] == "failed" and "local changes" in out["reason"]
    assert (wt / "a.txt").read_text() == "operator edit\n"
    assert rig.pm.status(100) == "Blocked"


# ---- push stays the operator's ------------------------------------------------------------------

def remote_with_upstream(env):
    bare = env.tmp / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(env.repo), str(bare)], check=True)
    sh(env.repo, "remote", "add", "origin", str(bare))
    sh(env.repo, "fetch", "-q", "origin")
    sh(env.repo, "branch", "-q", "-u", "origin/master", "master")
    return bare


def test_ahead_of_upstream_opens_one_push_request_awaiting_approval(env, rig):
    bare = remote_with_upstream(env)
    remote_before = sh(bare, "rev-parse", "master")
    q = rig.queue(env.project())
    q.merge(Ctx(), [env.task("1")])
    out = rig.gate.results[-1]
    assert out["result"] == "closed"
    [req] = rig.pm.created
    assert req["lane"] == "manager" and req["board"] == "board-p" and req["type"] == "Task"
    assert "Push `master`" in req["subject"] and "never does it" in req["description"]
    assert rig.pm.status(req["id"]) == "Awaiting approval"
    assert out["push_request"] == req["id"]
    assert "1 commit(s) ahead of `origin/master`" in [c for i, s, c in rig.pm.moves if i == 100][0]
    assert rig.kinds() == ["push_request", "feature_complete"]
    assert sh(bare, "rev-parse", "master") == remote_before       # nothing was pushed

    # a second Feature finishing while the request is open comments on it, no second request
    rig.pm.add(200, "Feature", "In Progress", None, "Milestone two")
    rig.pm.add(6, "Task", "In Progress", 200)
    q.merge(Ctx(), [env.task("6", content="six\n", fname="six.txt")])
    assert len(rig.pm.created) == 1
    assert rig.pm.comments[-1][0] == req["id"] and "Feature #200" in rig.pm.comments[-1][1]
    assert sh(bare, "rev-parse", "master") == remote_before

    # once the operator closes it, the next milestone opens a new one
    rig.pm.wps[req["id"]]["status"] = "Done"
    rig.pm.add(300, "Feature", "In Progress", None, "Milestone three")
    rig.pm.add(7, "Task", "In Progress", 300)
    q.merge(Ctx(), [env.task("7", content="seven\n", fname="seven.txt")])
    assert len(rig.pm.created) == 2


def test_level_with_upstream_needs_no_push_request(env, rig):
    remote_with_upstream(env)
    rig.pm.wps[1]["status"] = "Done"
    g = ms.MilestoneGate(project_of=lambda pid: env.project(), pm=rig.pm,
                         state_dir=str(env.state_dir), alert=lambda c, k, t: rig.alerts.append((k, t)))
    out = g(Ctx(), {"task": "1", "result": "merged", "branch": "feat/none"})
    assert out["result"] == "closed" and "push_request" not in out
    assert "level with `origin/master`" in out["comment"]


def test_push_status_with_a_remote_but_no_upstream(env):
    sh(env.repo, "remote", "add", "origin", str(env.tmp / "nowhere.git"))
    needed, why = ms.push_status(str(env.repo), "master")
    assert needed and "no upstream" in why


# ---- robustness ---------------------------------------------------------------------------------

def test_a_tracker_error_is_an_event_and_the_queue_goes_on(env, rig):
    class Broken(FakePM):
        def get(self, i):
            raise RuntimeError("nomad-pm down")
    rig.pm = Broken()
    q = rig.queue(env.project())
    ctx = Ctx()
    res = q.merge(ctx, [env.task("1")])
    assert res[0]["result"] == "merged"
    assert rig.gate.results[-1]["result"] == "error"
    assert any(k == "error" and "nomad-pm down" in m for k, m in ctx.events)
    assert rig.kinds() == ["milestone_failed"]


def test_the_default_alert_is_a_project_event(env, rig):
    rig.pm.wps[1]["status"] = "Done"
    g = ms.MilestoneGate(project_of=lambda pid: env.project(), pm=rig.pm, state_dir=str(env.state_dir))
    ctx = Ctx()
    g(ctx, {"task": "1", "result": "merged", "branch": "feat/none"})
    assert any(k == "alert" and m.startswith("feature_complete: Feature #100") for k, m in ctx.events)


# ---- gate_network (#1530): one code path for every supervisor gate run --------------------------

@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap not installed")
def test_gate_network_none_covers_reruns_post_test_and_the_milestone_gate(env, rig):
    env.config(tests={"T.A1_ok": "pass", "T.BLK6_flaky": "flaky"})
    project = env.project(flakes=["BLK6"])
    project = dataclasses.replace(project, autopilot=dataclasses.replace(
        project.autopilot, gate_network="none"))
    seen = []

    def runner(argv, cwd, log_path, timeout):
        seen.append((argv, log_path))
        return merge_mod.run_command(argv, cwd, log_path, timeout)

    q = rig.env.queue(project, runner=runner)
    rig.gate = ms.with_milestones(q, pm=rig.pm, board_of=lambda pid: "board-p",
                                  alert=lambda ctx, kind, text: rig.alerts.append((kind, text)))
    res = q.merge(Ctx(), [env.task("1")])
    assert res[0]["result"] == "merged" and rig.gate.results[-1]["result"] == "closed"
    logs = [os.path.basename(p) for _, p in seen]
    assert any("rerun-" in n for n in logs) and any("post-test" in n for n in logs)
    assert any(f"{os.sep}milestone{os.sep}" in p for _, p in seen)
    assert all(os.path.basename(a[0]) == "bwrap" and "--unshare-net" in a for a, _ in seen)
