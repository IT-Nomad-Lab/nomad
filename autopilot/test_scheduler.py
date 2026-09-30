"""Tests for the composed scheduler (#868 states and crash recovery, #867 caps and locks): the real
Supervisor + AutopilotScheduler (TaskSelection + WorkerPool + the generated permission profile),
a fake `claude`, a temp git repo and a fake nomad-pm client. No network, no real claude.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest autopilot -q
"""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, NOMAD)

from autopilot import permissions as P  # noqa: E402
from autopilot import projects  # noqa: E402
from autopilot import state as st  # noqa: E402
from autopilot import supervisor as sv  # noqa: E402
from autopilot import worker as wk  # noqa: E402
from autopilot.projects import Autopilot, Command, Project  # noqa: E402
from autopilot.scheduler import AutopilotScheduler  # noqa: E402

LANE = "customField4"

FAKE = r'''#!{python}
import json, os, re, subprocess, sys, time
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
tid = re.search(r"nomad-pm #(\d+)", prompt).group(1)
scenario = os.environ.get("FAKE_SCENARIO", "merge")
per_task = os.environ.get("FAKE_SCENARIO_" + tid)
scenario = per_task or scenario
out = os.environ.get("FAKE_OUT")
if out:
    with open(os.path.join(out, tid + ".argv.json"), "w") as f:
        json.dump({{"argv": argv, "cwd": os.getcwd()}}, f)

def emit(o):
    print(json.dumps(o), flush=True)

def bash(i, cmd, result, err=False):
    emit({{"type": "assistant", "message": {{"content": [
        {{"type": "tool_use", "id": i, "name": "Bash", "input": {{"command": cmd}}}}]}}}})
    emit({{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "tool_use_id": i, "is_error": err, "content": result}}]}}}})

emit({{"type": "system", "subtype": "init", "cwd": os.getcwd()}})
if scenario == "sleep":
    time.sleep(600)
denials, text = [], "Done.\n\nautopilot: result ready to merge"
if scenario == "refused":
    bash("t1", "git status", "Permission to use Bash has been denied.", True)
    denials = [{{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {{"command": "git status"}}}}]
    text = "Bash is denied, so I stopped.\n\nautopilot: result failed"
elif scenario == "nomarker":
    bash("t1", "git status", "clean")
    text = "I stopped."
else:
    bash("t1", "git status", "clean")
    if scenario == "wrapper":
        w = os.environ["FAKE_WRAPPER"]
        p = subprocess.run([w], capture_output=True, text=True)
        bash("t2", w, p.stdout + p.stderr, p.returncode != 0)
    status_dir = os.environ.get("FAKE_STATUS_DIR")
    if status_dir:
        with open(os.path.join(status_dir, tid), "w") as f:
            f.write("In Review")
emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": text}}]}}}})
emit({{"type": "result", "subtype": "success", "is_error": False, "result": text,
      "total_cost_usd": 0.25, "permission_denials": denials}})
'''

SLOW = """#!/bin/sh
# test command: records when it held the lock
echo "start $(pwd) $(date +%s.%N)" >> "$LOCK_TRACE"
sleep 0.6
echo "end $(pwd) $(date +%s.%N)" >> "$LOCK_TRACE"
"""


# ---- fixtures -------------------------------------------------------------------------------

def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path):
    os.makedirs(os.path.join(path, "scripts"))
    sh(path, "git", "init", "-q", "-b", "main")
    sh(path, "git", "config", "user.email", "t@example.com")
    sh(path, "git", "config", "user.name", "t")
    with open(os.path.join(path, "README"), "w") as f:
        f.write("hi\n")
    slow = os.path.join(path, "scripts", "slow.sh")
    with open(slow, "w") as f:
        f.write(SLOW)
    os.chmod(slow, 0o755)
    sh(path, "git", "add", ".")
    sh(path, "git", "commit", "-qm", "init")
    return path


def make_project(tmp_path, pid="proj", cap=3, gpu=False, lock=None):
    repo = make_repo(str(tmp_path / pid))
    lock = lock or str(tmp_path / "playmode.lock")
    play = Command("scripts/slow.sh {filter}", (lock,))
    ap = Autopilot(test={"PlayMode": play}, pre_merge_gate=(play,), milestone_gate=(play,),
                   locks=(lock,), cap=cap, worktree_root=str(tmp_path / f"{pid}-wt"),
                   requires_gpu=gpu)
    return Project(id=pid, path=repo, autopilot=ap)


def wp(i, status="Ready", description="", subject=None):
    return {"id": i, "subject": subject or f"task {i}", "updatedAt": "2026-09-28T00:00:00Z",
            "description": {"raw": description},
            "_links": {"status": {"title": status}, "type": {"title": "Task"},
                       "priority": {"title": "Normal"}, LANE: {"title": "dev"}}}


class FakePM:
    """The PMClient surface over an in-memory board. Workers (the fake claude) move a task by
    writing its new status into `status_dir/<id>`; `fail` makes every read raise."""
    CLOSED = {"Done", "Won't do", "Duplicate"}

    def __init__(self, status_dir, wps=(), relations=()):
        self.status_dir = str(status_dir)
        os.makedirs(self.status_dir, exist_ok=True)
        self.wps = {w["id"]: w for w in wps}
        self.relations = list(relations)
        self.notes = {}
        self.fail = None

    def set_status(self, i, status):
        with open(os.path.join(self.status_dir, str(i)), "w") as f:
            f.write(status)

    def status(self, i):
        f = os.path.join(self.status_dir, str(i))
        return open(f).read().strip() if os.path.exists(f) else self.wps[i]["_links"]["status"]["title"]

    def _view(self, w):
        v = json.loads(json.dumps(w))
        v["_links"]["status"]["title"] = self.status(w["id"])
        return v

    def open_work_packages(self, board):
        if self.fail:
            raise self.fail
        return [self._view(w) for w in self.wps.values() if not self.closed(self.status(w["id"]))]

    def lane_key(self, board):
        return LANE

    def block_relations(self):
        return list(self.relations)

    def work_package(self, i):
        return self._view(self.wps[i])

    def closed(self, status):
        return status in self.CLOSED

    def comments(self, i):
        return list(self.notes.get(i, []))

    def comment(self, i, text):
        self.notes.setdefault(i, []).append(text)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = tmp_path / "claude"
    f.write_text(FAKE.format(python=sys.executable))
    f.chmod(0o755)
    out = tmp_path / "fake-out"
    out.mkdir()
    monkeypatch.setenv("FAKE_OUT", str(out))
    monkeypatch.setenv("FAKE_STATUS_DIR", str(tmp_path / "board"))
    monkeypatch.setenv("FAKE_SCENARIO", "merge")
    return str(f)


class Rig:
    """One supervisor over one state file, a fake board, and real workers."""

    def __init__(self, tmp_path, fake, projects_by_id, pm, merged=None):
        self.tmp = tmp_path
        self.fake = fake
        self.projects = projects_by_id
        self.pm = pm
        self.merged = [] if merged is None else merged
        self.state_path = str(tmp_path / "state" / "state.json")
        self.boot()

    def merger(self, ctx, finished):
        for f in finished:
            if f["outcome"] == "merge":        # the merge queue (#871), stubbed: mark it Done
                self.merged.append(f["task"])
                self.pm.set_status(int(f["task"]), "Done")

    def boot(self):
        """A (re)started supervisor: fresh pool, scheduler and store over the same files."""
        self.store = st.StateStore(self.state_path)
        self.pool = wk.WorkerPool(project_of=self.projects.get, claude=self.fake,
                                  state_dir=str(self.tmp / "state"))
        self.sched = AutopilotScheduler(pm=self.pm, registry=self.projects.get, pool=self.pool,
                                        merger=self.merger)
        self.sup = sv.Supervisor(self.store, self.sched)
        return self

    def project(self, pid="proj"):
        return self.store.project(pid)

    def until(self, cond, timeout=20, pid="proj"):
        end = time.time() + timeout
        while time.time() < end:
            self.sup.run_all()
            if cond(self.project(pid)):
                return self.project(pid)
            time.sleep(0.05)
        raise AssertionError(f"timed out; project is {self.project(pid)}")

    def kill_all(self):
        for pid in self.projects:
            self.pool.stop(sv.PassContext(self.store, pid, {}, self.store.project(pid)), "kill")


@pytest.fixture
def one(tmp_path, fake):
    p = make_project(tmp_path)
    pm = FakePM(tmp_path / "board", [
        wp(101), wp(102, "New", "**Acceptance criteria:** it works."),
        wp(103, "New"),                                     # no criteria: left alone
    ])
    rig = Rig(tmp_path, fake, {"proj": p}, pm)
    yield rig
    rig.kill_all()


# ---- end to end -----------------------------------------------------------------------------

def test_end_to_end_two_workers_merge_then_idle(one, tmp_path):
    rig = one
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    p = rig.project()
    assert sorted(w["task"] for w in p["workers"]) == ["101", "102"]
    assert p["state"] == "running" and p["detail"].startswith("2 worker(s): #101, #102")

    p = rig.until(lambda p: p["state"] == "idle")
    assert sorted(rig.merged) == ["101", "102"] and p["workers"] == []
    assert p["detail"] == "nothing to do (1 New without acceptance criteria: #103)"
    assert rig.store.spent_today() == pytest.approx(0.5)
    kinds = [(e["kind"], e["msg"]) for e in p["events"]]
    assert ("finished", "task 101: merge") in kinds and ("finished", "task 102: merge") in kinds

    # each worker ran with the generated profile, in its own worktree
    for tid in ("101", "102"):
        seen = json.load(open(tmp_path / "fake-out" / f"{tid}.argv.json"))
        argv = seen["argv"]
        assert seen["cwd"] == str(tmp_path / "proj-wt" / f"ap-{tid}")
        i = argv.index("--settings")
        assert argv[i + 1] == str(tmp_path / "state" / "settings" / f"proj-{tid}.json")
        assert argv[i + 2:i + 4] == ["--setting-sources", ""]
        assert str(tmp_path / "state" / "bin" / "proj-test-PlayMode") in argv[1]
    new_prompt = json.load(open(tmp_path / "fake-out" / "102.argv.json"))["argv"][1]
    ready_prompt = json.load(open(tmp_path / "fake-out" / "101.argv.json"))["argv"][1]
    assert "promote it to Ready" in new_prompt and "promote" not in ready_prompt

    # run records are marked collected, so a restart does not find them again
    recs = [json.load(open(f)) for f in (tmp_path / "state" / "runs" / "proj").glob("*.run.json")]
    assert sorted(r["collected"] for r in recs) == ["merge", "merge"]


def test_main_composes_the_real_scheduler(monkeypatch):
    monkeypatch.setattr(sv, "NOMAD", NOMAD)
    import autopilot.tasks as tk
    monkeypatch.setattr(tk, "PMClient", lambda: FakePM("/tmp/unused-fake-board"))
    sch = sv.build_scheduler()
    assert isinstance(sch, AutopilotScheduler)
    assert sch._registry is projects.hub_get and sch.pool.project_of is projects.hub_get
    assert sch.pool.settings_for.__qualname__.startswith("generated_settings")
    assert sch.drafter is not None and sch.drafter.pm is sch.pm      # criteria drafts (#889)


# ---- states (#868) --------------------------------------------------------------------------

def test_first_command_refused_is_a_sticky_error_until_run_is_pressed(one, monkeypatch):
    rig = one
    monkeypatch.setenv("FAKE_SCENARIO", "refused")
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    p = rig.until(lambda p: p["state"] == "error" and p["workers"] == [])
    assert "first shell command refused" in p["detail"] and "press Run again" in p["detail"]
    assert p["fault"] and p["workers"] == []
    rig.sup.run_all()                                   # tasks are still Ready: no new worker
    assert rig.project()["workers"] == [] and rig.project()["state"] == "error"

    monkeypatch.setenv("FAKE_SCENARIO", "merge")
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")                       # the operator fixed it and pressed Run
    assert rig.project()["fault"] == ""
    rig.sup.run_all()
    assert len(rig.project()["workers"]) == 2


def test_nomad_pm_unreachable_is_error(one):
    one.pm.fail = ConnectionError("connection refused")
    one.sup.store.set_autopilot("proj", True)
    one.sup.start_project("proj")
    one.sup.run_all()
    p = one.project()
    assert (p["state"], p["detail"]) == ("error", "nomad-pm unreachable: ConnectionError: connection refused")
    one.pm.fail = None
    one.sup.run_all()
    assert one.project()["state"] == "running"          # transient: the next pass recovers


def test_repo_unreachable_is_error(one, tmp_path):
    os.rename(tmp_path / "proj", tmp_path / "proj-moved")
    one.sup.store.set_autopilot("proj", True)
    one.sup.start_project("proj")
    one.sup.run_all()
    p = one.project()
    assert p["state"] == "error" and p["detail"].startswith("repo unreachable:")


def test_a_project_entry_the_generator_refuses_is_error(tmp_path, fake):
    p = make_project(tmp_path)
    bad = Project(id="proj", path=p.path, autopilot=Autopilot(
        test={"default": Command("git push origin main")}, pre_merge_gate=(), milestone_gate=(),
        worktree_root=p.autopilot.worktree_root))
    rig = Rig(tmp_path, fake, {"proj": bad}, FakePM(tmp_path / "board", [wp(1)]))
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    pr = rig.project()
    assert pr["state"] == "error" and "worker profile not generated" in pr["detail"]
    assert not os.path.exists(os.path.join(p.autopilot.worktree_root, "ap-1"))   # nothing made


def test_no_nomad_pm_to_hand_over_is_error(tmp_path, fake, mcp_source):
    mcp_source.write_text('{"mcpServers": {}}')       # #832/#842: a run with no tracker
    p = make_project(tmp_path)
    rig = Rig(tmp_path, fake, {"proj": p}, FakePM(tmp_path / "board", [wp(1)]))
    rig.sup.start_project("proj")
    rig.sup.run_all()
    pr = rig.project()
    assert pr["state"] == "error" and "nomad-pm" in pr["detail"]
    assert not os.path.exists(os.path.join(p.autopilot.worktree_root, "ap-1"))


def test_waiting_and_needs_you_name_the_tasks(tmp_path, fake):
    p = make_project(tmp_path)
    pm = FakePM(tmp_path / "board", [wp(1, "In Progress"), wp(2), wp(3, "Awaiting approval")],
                relations=[(1, 2)])
    rig = Rig(tmp_path, fake, {"proj": p}, pm)
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    pr = rig.project()
    assert (pr["state"], pr["detail"]) == ("needs_you", "#3 needs operator: Awaiting approval")
    pm.set_status(3, "Done")
    rig.sup.run_all()
    pr = rig.project()
    assert (pr["state"], pr["detail"]) == ("waiting", "#2 blocked_by #1 (In Progress)")


def test_running_detail_names_what_else_waits(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "sleep")
    p = make_project(tmp_path, cap=1)
    pm = FakePM(tmp_path / "board", [wp(1), wp(2), wp(3, "Awaiting approval")])
    rig = Rig(tmp_path, fake, {"proj": p}, pm)
    try:
        rig.sup.store.set_autopilot("proj", True)
        rig.sup.start_project("proj")
        rig.sup.run_all()
        pr = rig.project()
        assert pr["state"] == "running"
        assert pr["detail"] == ("1 worker(s): #1; 1 more queued on the cap: #2; "
                                "needs you: #3 needs operator: Awaiting approval")
    finally:
        rig.kill_all()


def test_budget_paused_with_the_real_composition(one):
    one.store.update_config({"daily_cap_usd": 0.1})
    one.store.add_spend(0.2)
    one.sup.store.set_autopilot("proj", True)
    one.sup.start_project("proj")
    one.sup.run_all()
    p = one.project()
    assert p["state"] == "budget_paused" and p["workers"] == [] and "2 task(s) wait" in p["detail"]


def test_a_failed_run_that_left_the_task_ready_is_benched(one, monkeypatch):
    """A run that ends `failed` without moving its task would restart every pass. It is held
    back and shown as needing the operator until Run is pressed again."""
    rig = one
    monkeypatch.setenv("FAKE_SCENARIO_101", "nomarker")
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    p = rig.until(lambda p: p["workers"] == [] and p["state"] != "running")
    assert rig.merged == ["102"]
    assert p["state"] == "needs_you"
    assert p["detail"].startswith("#101 last run failed: no `autopilot: result` marker")
    assert p["detail"].endswith("press Run again to retry it")
    rig.sup.run_all()
    assert rig.project()["workers"] == []               # still held back
    monkeypatch.delenv("FAKE_SCENARIO_101")
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")                       # Run pressed: retried
    rig.sup.run_all()
    assert [w["task"] for w in rig.project()["workers"]] == ["101"]


# ---- stop and draining (#866 note) ----------------------------------------------------------

def test_stop_kill_collects_as_resume_and_tells_the_tracker(one, monkeypatch):
    rig = one
    monkeypatch.setenv("FAKE_SCENARIO", "sleep")
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    assert len(rig.project()["workers"]) == 2
    rig.sup.stop_project("proj", "kill")
    p = rig.until(lambda p: p["workers"] == [])
    assert p["state"] == "off" and p["detail"] == "stopped (kill); all workers collected"
    for tid in (101, 102):
        [note] = rig.pm.notes[tid]
        assert note.startswith("autopilot: resume. killed by the operator")
    # nothing new starts while off, and the tasks now qualify as resume for the next Run
    rig.sup.run_all()
    assert rig.project()["workers"] == []
    monkeypatch.setenv("FAKE_SCENARIO", "merge")
    rig.pm.set_status(101, "In Progress")
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    assert "101" in [w["task"] for w in rig.project()["workers"]]


def test_stop_finish_keeps_collecting_until_empty(one, monkeypatch):
    rig = one
    monkeypatch.setenv("FAKE_SCENARIO", "sleep")
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    rig.sup.stop_project("proj", "finish")
    rig.sup.run_all()
    p = rig.project()
    assert p["state"] == "off" and len(p["workers"]) == 2
    assert p["detail"] == "stopped (finish); 2 worker(s) still finishing"
    for w in p["workers"]:                              # they end by themselves
        os.killpg(w["pid"], signal.SIGTERM)
    p = rig.until(lambda p: p["workers"] == [])
    assert p["state"] == "off" and p["detail"] == "stopped (finish); all workers collected"
    assert [e for e in p["events"] if e["kind"] == "started"] and not rig.merged


# ---- crash recovery (#868, AC8) -------------------------------------------------------------

def test_recovery_adopts_a_live_run_and_collects_it_later(one, monkeypatch):
    rig = one
    monkeypatch.setenv("FAKE_SCENARIO", "sleep")
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    before = {w["task"]: w for w in rig.project()["workers"]}
    assert len(before) == 2

    rig.boot()                                          # the supervisor was killed and restarted
    rig.sup.recover()
    p = rig.project()
    assert {w["task"] for w in p["workers"]} == set(before)
    assert len(rig.pool.live("proj")) == 2
    assert any("adopted" in e["msg"] for e in p["events"])
    rig.sup.run_all()
    assert rig.project()["state"] == "running" and len(rig.project()["workers"]) == 2

    rig.sup.stop_project("proj", "kill")                # adopted runs can still be killed
    p = rig.until(lambda p: p["workers"] == [])
    for w in before.values():
        assert wk.proc_start(w["pid"]) is None          # gone (a zombie of the old pool at most)
        assert sh(rig.projects["proj"].repo, "git", "rev-parse", "--verify", w["branch"])


def test_recovery_collects_a_run_that_finished_while_down(one):
    rig = one
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    workers = rig.project()["workers"]
    for w in workers:                                   # they finish; nobody collects them
        while wk.proc_start(w["pid"]) is not None:
            time.sleep(0.05)
    rig.boot()
    rig.sup.recover()
    assert len(rig.project()["workers"]) == 2
    p = rig.until(lambda p: p["state"] == "idle")
    assert sorted(rig.merged) == ["101", "102"] and p["workers"] == []
    assert not rig.pm.notes                             # judged from the log, not a resume


def test_recovery_marks_a_run_that_died_without_result_resume(one, monkeypatch):
    rig = one
    monkeypatch.setenv("FAKE_SCENARIO", "sleep")
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    workers = rig.project()["workers"]
    for w in workers:                                   # died with the supervisor (same cgroup)
        os.killpg(w["pid"], signal.SIGKILL)
        while wk.proc_start(w["pid"]) is not None:
            time.sleep(0.05)
    rig.boot()
    rig.sup.recover()
    rig.sup.run_all()
    p = rig.project()
    finished = [e["msg"] for e in p["events"] if e["kind"] == "finished"]
    assert len(finished) == 2 and all(": resume (the run ended without a result" in m for m in finished)
    for tid in (101, 102):
        assert "while the supervisor was down" in rig.pm.notes[tid][0]
    assert all(os.path.isdir(w["worktree"]) for w in workers)


def test_recovery_finds_a_run_the_state_file_never_saw(one, monkeypatch):
    """Crash between the worker's launch and the pass writing the state file."""
    rig = one
    monkeypatch.setenv("FAKE_SCENARIO", "sleep")
    rig.store.set_autopilot("proj", True)
    rig.store.start("proj")
    run = rig.pool.launch(rig.projects["proj"], {"task": 101})
    assert rig.project()["workers"] == []
    rig.boot()
    rig.sup.recover()
    assert [w["task"] for w in rig.project()["workers"]] == ["101"]
    assert rig.pool.live("proj")[0].proc.pid == run.proc.pid
    rig.kill_all()


def test_recovery_reports_a_leftover_worktree(one):
    rig = one
    wk.ensure_worktree(rig.projects["proj"].repo, rig.projects["proj"].autopilot.worktree_root,
                       "77", "old work")
    rig.store.set_autopilot("proj", True)
    rig.store.start("proj")
    rig.boot()
    rig.sup.recover()
    msgs = [e["msg"] for e in rig.project()["events"] if e["kind"] == "recover"]
    assert any("ap-77 on feat/77-old-work kept" in m for m in msgs)


def test_proc_start_tells_a_recycled_pid_apart():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        s = wk.proc_start(p.pid)
        assert s is not None
        assert wk.Adopted(p.pid, s).poll() is None
        assert wk.Adopted(p.pid, s + 1).poll() == -1    # same pid, other start time: not ours
    finally:
        p.kill()
        p.wait()
    assert wk.proc_start(p.pid) is None


# ---- caps (#867) ----------------------------------------------------------------------------

def test_project_cap_from_projects_yaml_and_global_cap(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "sleep")
    a = make_project(tmp_path, "a", cap=2)
    b = make_project(tmp_path, "b")                     # default cap 3
    c = make_project(tmp_path, "c")
    board = [wp(i) for i in range(1, 6)]

    class Boards(FakePM):
        pass
    pm = Boards(tmp_path / "board", board)
    rig = Rig(tmp_path, fake, {"a": a, "b": b, "c": c}, pm)
    try:
        for pid in ("a", "b", "c"):
            rig.sup.store.set_autopilot(pid, True)
            rig.sup.start_project(pid)
        rig.sup.run_all()
        counts = {pid: len(rig.project(pid)["workers"]) for pid in ("a", "b", "c")}
        assert counts == {"a": 2, "b": 3, "c": 1}         # 2 (yaml cap) + 3 (default) + 1 = 6
        assert rig.project("c")["detail"].startswith("1 worker(s): #1; 4 more queued")
        rig.store.update_config({"per_project_cap": 1})   # the cockpit lowers every project
        assert rig.sup.project_cap(sv.PassContext(rig.store, "a", {}, {}), rig.store.config()) == 1
    finally:
        rig.kill_all()


def test_global_cap_full_elsewhere_is_waiting(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "sleep")
    a, b = make_project(tmp_path, "a"), make_project(tmp_path, "b")
    rig = Rig(tmp_path, fake, {"a": a, "b": b}, FakePM(tmp_path / "board", [wp(1), wp(2)]))
    rig.store.update_config({"global_cap": 2})
    try:
        rig.sup.store.set_autopilot("a", True)
        rig.sup.start_project("a")
        rig.sup.store.set_autopilot("b", True)
        rig.sup.start_project("b")
        rig.sup.run_all()
        pb = rig.project("b")
        assert pb["workers"] == []
        assert (pb["state"], pb["detail"]) == ("waiting", "2 task(s) ready, every worker slot is taken (global cap)")
    finally:
        rig.kill_all()


# ---- shared locks (#867) --------------------------------------------------------------------

def _intervals(trace):
    starts, out = {}, []
    for line in open(trace):
        kind, who, t = line.split()
        if kind == "start":
            starts[who] = float(t)
        else:
            out.append((starts.pop(who), float(t), who))
    return sorted(out)


def test_two_workers_of_one_project_queue_on_its_lock(tmp_path, fake, monkeypatch):
    p = make_project(tmp_path)
    trace = tmp_path / "trace"
    monkeypatch.setenv("LOCK_TRACE", str(trace))
    monkeypatch.setenv("FAKE_SCENARIO", "wrapper")
    monkeypatch.setenv("FAKE_WRAPPER", str(tmp_path / "state" / "bin" / "proj-test-PlayMode"))
    rig = Rig(tmp_path, fake, {"proj": p}, FakePM(tmp_path / "board", [wp(1), wp(2)]))
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    assert len(rig.project()["workers"]) == 2           # both run at once...
    rig.until(lambda pr: pr["state"] == "idle")
    iv = _intervals(trace)
    assert len(iv) == 2 and {os.path.basename(w) for _, _, w in iv} == {"ap-1", "ap-2"}
    assert iv[0][1] <= iv[1][0]                         # ...but the locked test runs one by one
    script = open(tmp_path / "state" / "bin" / "proj-test-PlayMode").read()
    assert f"exec 9>>{tmp_path / 'playmode.lock'}\nflock 9" in script


def test_gpu_projects_share_the_gpu_lock(tmp_path, monkeypatch):
    gpu = str(tmp_path / "gpu" / "nomad-gpu.lock")
    os.makedirs(os.path.dirname(gpu))
    monkeypatch.setattr(P, "GPU_LOCK", gpu)
    assert P.GPU_LOCK != "/tmp/x" and P.GPU_LOCK.endswith("nomad-gpu.lock")
    trace = tmp_path / "trace"
    monkeypatch.setenv("LOCK_TRACE", str(trace))
    procs = []
    for pid in ("g1", "g2"):
        proj = make_project(tmp_path, pid, gpu=True, lock=str(tmp_path / f"{pid}.lock"))
        state_dir = str(tmp_path / f"state-{pid}")
        wt = wk.ensure_worktree(proj.repo, proj.autopilot.worktree_root, "1", "x")
        _, wrappers = P.write(proj, wt.path, state_dir=state_dir, task_id=1)
        w = next(w for w in wrappers if w.name == f"{pid}-test-PlayMode")
        assert gpu in w.script and f"{pid}.lock" in w.script
        procs.append(subprocess.Popen([w.path], cwd=wt.path))
    for pr in procs:
        assert pr.wait(timeout=20) == 0
    iv = _intervals(trace)
    assert len(iv) == 2 and iv[0][1] <= iv[1][0]        # different projects, one GPU


def test_the_gpu_lock_is_the_documented_one():
    assert P.GPU_LOCK == "/tmp/nomad-gpu.lock"
    doc = open(os.path.join(NOMAD, "docs", "SHARED-RESOURCES.md")).read()
    assert "/tmp/nomad-gpu.lock" in doc


# ---- the registry is the hub's, wherever the code runs --------------------------------------

REG = """projects:
  - id: runematch
    path: runematch
    autopilot:
      test: pytest -q
      cap: 2
"""


def test_registry_resolves_against_the_hub_not_the_worktree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "nomad").mkdir(parents=True)
    (home / "runematch").mkdir()
    (home / "nomad" / "projects.yaml").write_text(REG)
    wt_copy = home / "nomad-wt" / "autopilot"
    wt_copy.mkdir(parents=True)
    (wt_copy / "projects.yaml").write_text(REG)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NOMAD_PROJECTS_FILE", raising=False)
    monkeypatch.delenv("NOMAD_PROJECTS_BASE", raising=False)

    assert projects.hub_file() == str(home / "nomad" / "projects.yaml")
    p = projects.hub_get("runematch")
    assert p.path == str(home / "runematch") and p.autopilot.cap == 2

    # an override file in a worktree still names the real repos under ~
    monkeypatch.setenv("NOMAD_PROJECTS_FILE", str(wt_copy / "projects.yaml"))
    p = projects.hub_get("runematch")
    assert p.path == str(home / "runematch")
    # the dispatcher's own rule, for contrast, resolves against the file's grandparent
    assert projects.get("runematch", str(wt_copy / "projects.yaml")).path == str(home / "nomad-wt" / "runematch")


# ---- the merge queue in task selection (#871) --------------------------------------------------

class _Queue:
    def __init__(self, busy):
        self._busy = busy
        self.calls = []

    def merge(self, ctx, finished):
        self.calls.append(finished)

    def busy(self, pid):
        return list(self._busy)


def test_merger_object_and_busy_lookup():
    from autopilot.scheduler import AutopilotScheduler
    q = _Queue(["12", 5])
    s = AutopilotScheduler(pm=object(), registry=lambda pid: None, pool=object(), merger=q)
    s.merge(None, [])
    assert q.calls == [[]]
    assert s._merging("a") == {"12", "5"}
    calls = []
    s2 = AutopilotScheduler(pm=object(), registry=lambda pid: None, pool=object(),
                            merger=lambda ctx, f: calls.append(f))
    s2.merge(None, [1])
    assert calls == [[1]] and s2._merging("a") == set()


def test_runnable_lists_autopilot_projects_with_their_board():
    """#883: the cockpit's Run buttons. Only an entry with an autopilot block and a board."""
    from autopilot.scheduler import AutopilotScheduler
    ap = Autopilot(test={"t": Command("true", ())}, pre_merge_gate=(), milestone_gate=(), locks=())
    entries = [Project(id="runematch", path="/x", autopilot=ap),
               Project(id="civis-poc", path="/x", autopilot=ap),        # board civis
               Project(id="nomad-public", path="/x", autopilot=ap),     # no board
               Project(id="job-search", path="/x", autopilot=None)]     # no autopilot block
    s = AutopilotScheduler(pm=object(), registry=lambda pid: None, pool=object(),
                           listing=lambda: entries)
    assert s.runnable() == {"runematch": "runematch", "civis-poc": "civis"}
    assert s.board_of("nomad") == "nomad-work" and s.board_of("nomad-public") is None


# ---- run mode "once" with the real composition (#890) -----------------------------------------

class _HeldQueue:
    """A merge queue that holds every `merge` until `release`, then reports `merged`."""

    def __init__(self, pm, held=None, results=None):
        self.pm = pm
        self.held = set() if held is None else held
        self.results = {} if results is None else results

    def merge(self, ctx, finished):
        for f in finished:
            if f["outcome"] == "merge":
                self.held.add(str(f["task"]))

    def busy(self, pid):
        return sorted(self.held)

    def result(self, pid, tid):
        return self.results.get(str(tid))

    def release(self, tid, result="merged"):
        self.held.discard(str(tid))
        self.results[str(tid)] = result
        if result == "merged":
            self.pm.set_status(int(tid), "Done")


def test_switch_off_runs_the_top_task_once_through_the_merge_queue(tmp_path, fake):
    """Two Ready tasks: OFF starts exactly #201 (the top one), holds while the queue merges it,
    starts nothing else, then turns itself off, also across a supervisor restart."""
    p = make_project(tmp_path)
    pm = FakePM(tmp_path / "board", [wp(201), wp(202)])
    rig = Rig(tmp_path, fake, {"proj": p}, pm)
    q = _HeldQueue(pm)
    rig.sched.merger = q
    try:
        rig.sup.start_project("proj")                   # the switch is OFF: one task
        rig.sup.run_all()
        pr = rig.project()
        assert [w["task"] for w in pr["workers"]] == ["201"] and pr["run_mode"] == "once"
        pr = rig.until(lambda pr: "in the merge queue" in pr["detail"])
        assert pr["enabled"] and pr["state"] == "running" and pr["workers"] == []
        rig.sup.run_all()
        assert rig.project()["workers"] == []           # #202 qualifies and still does not start

        rig.boot()                                      # restart while the merge is pending
        rig.sched.merger = _HeldQueue(pm, q.held, q.results)
        rig.sup.recover()
        rig.sup.run_all()
        pr = rig.project()
        assert pr["enabled"] and pr["once"] == {"task": "201", "phase": "merging"}
        assert pr["workers"] == []

        rig.sched.merger.release("201")
        rig.sup.run_all()
        pr = rig.project()
        assert (pr["enabled"], pr["state"], pr["detail"]) == (False, "off", "one task done: #201 merged")
        rig.sup.run_all()
        assert rig.project()["workers"] == [] and not os.path.exists(tmp_path / "fake-out" / "202.argv.json")
    finally:
        rig.kill_all()


def test_switch_off_with_a_failed_run_is_off_with_that_outcome(one, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "nomarker")
    one.sup.start_project("proj")
    pr = one.until(lambda pr: not pr["enabled"])
    assert pr["state"] == "off" and pr["detail"] == "one task done: #101 failed"
    assert not os.path.exists(os.path.join(os.environ["FAKE_OUT"], "102.argv.json"))


def test_switch_off_with_nothing_ready_is_off_with_the_reason(tmp_path, fake):
    p = make_project(tmp_path)
    pm = FakePM(tmp_path / "board", [wp(301, "New")])
    rig = Rig(tmp_path, fake, {"proj": p}, pm)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    pr = rig.project()
    assert (pr["enabled"], pr["state"]) == (False, "off")
    assert pr["detail"].startswith("one task: nothing qualifies (nothing to do (1 New without")


def test_merge_result_hook():
    q = _Queue(["7"])
    q.result = lambda pid, tid: {"8": "blocked"}.get(tid)
    s = AutopilotScheduler(pm=object(), registry=lambda pid: None, pool=object(), merger=q)
    ctx = sv.PassContext(None, "a", {}, {})
    assert s.merge_result(ctx, 7) is None
    assert s.merge_result(ctx, "8") == "blocked"
    assert s.merge_result(ctx, "9").startswith("merge result unknown")
    s2 = AutopilotScheduler(pm=object(), registry=lambda pid: None, pool=object(),
                            merger=lambda ctx, f: None)
    assert s2.merge_result(ctx, "9") == "merge"
