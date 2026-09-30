"""Tests for the supervisor: who may change it, whether /health tells the truth about the loop,
and that one bad pass cannot stop the loop (CLAUDE.md, "a process that never exits").

Run: python3 -m pytest autopilot/ -q
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import state as st  # noqa: E402
import supervisor as sv  # noqa: E402

TOKEN = "test-token"


@pytest.fixture
def store(tmp_path):
    return st.StateStore(str(tmp_path / "state.json"))


@pytest.fixture
def served(store):
    """A real server on an ephemeral port, loop not running unless a test starts it."""
    sup = sv.Supervisor(store, pass_secs=60)
    srv = sv.make_server(sup, TOKEN, port=0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield sup, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(base, method, path, body=None, token=TOKEN):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("X-Nomad-Token", token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---- auth -------------------------------------------------------------------------------------

@pytest.mark.parametrize("path,body", [
    ("/pass", None), ("/config", {"global_cap": 1}),
    ("/projects/a/start", None), ("/projects/a/stop", {"mode": "finish"}),
])
@pytest.mark.parametrize("token", [None, "wrong"])
def test_every_mutating_route_needs_the_token(served, path, body, token):
    sup, base = served
    code, out = call(base, "POST", path, body, token=token)
    assert code == 401 and not out["ok"]
    assert sup.store.enabled_projects() == []
    assert sup.store.config() == st.DEFAULT_CONFIG


def test_reads_are_open(served):
    _, base = served
    for path in ("/health", "/status", "/config"):
        assert call(base, "GET", path, token=None)[0] == 200


def test_token_env_wins_and_file_is_made_private(tmp_path, monkeypatch):
    p = str(tmp_path / "cfg" / "autopilot-token")
    monkeypatch.delenv("NOMAD_AUTOPILOT_TOKEN", raising=False)
    tok = sv.load_or_make_token(p)
    assert tok and oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert sv.load_or_make_token(p) == tok           # stable across restarts
    monkeypatch.setenv("NOMAD_AUTOPILOT_TOKEN", "from-env")
    assert sv.load_or_make_token(p) == "from-env"


# ---- routes -----------------------------------------------------------------------------------

def test_start_stop_over_http(served):
    sup, base = served
    code, out = call(base, "POST", "/projects/runematch/start")
    assert code == 200 and out["project"]["enabled"] and out["project"]["state"] == "idle"

    code, out = call(base, "POST", "/projects/runematch/stop", {"mode": "kill"})
    assert code == 200 and out["project"]["state"] == "off"
    assert "kill" in out["project"]["detail"]

    assert call(base, "POST", "/projects/runematch/stop", {"mode": "later"})[0] == 400
    assert call(base, "POST", "/projects/..%2F/start")[0] == 404
    status = call(base, "GET", "/status")[1]
    assert status["projects"]["runematch"]["enabled"] is False


def test_stop_hands_the_mode_to_the_scheduler(store):
    seen = []

    class S(sv.Scheduler):
        def stop(self, ctx, mode):
            seen.append((ctx.project_id, mode))
    sup = sv.Supervisor(store, S())
    sup.store.set_autopilot("a", True)
    sup.start_project("a")
    sup.stop_project("a", "finish")
    assert seen == [("a", "finish")]


def test_config_get_and_post(served):
    _, base = served
    code, out = call(base, "POST", "/config", {"budget_mode": "unlimited", "per_project_cap": 2})
    assert code == 200 and out["config"]["budget_mode"] == "unlimited"
    assert call(base, "GET", "/config")[1]["config"]["per_project_cap"] == 2
    assert call(base, "POST", "/config", {"budget_mode": "free"})[0] == 400
    assert call(base, "POST", "/config", {"global_cap": -2})[0] == 400


def test_pass_route_wakes_the_loop(served):
    sup, base = served
    sup.store.set_autopilot("a", True)
    sup.start_project("a")
    threading.Thread(target=sup.loop, daemon=True).start()
    assert wait_for(lambda: sup.passes >= 1)
    n = sup.passes
    code, _ = call(base, "POST", "/pass")
    assert code == 202
    assert wait_for(lambda: sup.passes > n, timeout=2)   # well inside the 60 s interval
    sup.halt()


# ---- heartbeat --------------------------------------------------------------------------------

def test_health_reports_heartbeat_age(served):
    sup, base = served
    sup.heartbeat = time.time() - 42
    code, out = call(base, "GET", "/health", token=None)
    assert code == 200 and out["ok"] is True
    assert 41 <= out["heartbeat_age_s"] <= 44

    sup.heartbeat = time.time() - 3 * sup.pass_secs - 5    # three missed passes: wedged
    out = call(base, "GET", "/health", token=None)[1]
    assert out["ok"] is False and out["heartbeat_age_s"] > 180


def test_a_pass_moves_the_heartbeat_and_last_pass(store):
    sup = sv.Supervisor(store)
    sup.heartbeat = 0
    sup.store.set_autopilot("a", True)
    sup.start_project("a")
    sup.run_all()
    assert time.time() - sup.heartbeat < 2
    p = store.project("a")
    assert p["last_pass"] and p["state"] == "idle"


# ---- the loop does not die --------------------------------------------------------------------

class Flaky(sv.Scheduler):
    """Raises on the first pass for project 'bad', then behaves."""
    def __init__(self):
        self.calls = {}

    def select_tasks(self, ctx):
        n = self.calls[ctx.project_id] = self.calls.get(ctx.project_id, 0) + 1
        if ctx.project_id == "bad" and n == 1:
            raise RuntimeError("nomad-pm unreachable")
        return []


def test_a_pass_exception_marks_error_and_the_loop_goes_on(store):
    sched = Flaky()
    sup = sv.Supervisor(store, sched, pass_secs=0.05)
    sup.store.set_autopilot("bad", True)
    sup.start_project("bad")
    sup.store.set_autopilot("good", True)
    sup.start_project("good")
    t = threading.Thread(target=sup.loop, daemon=True)
    t.start()

    # First pass: 'bad' errors, 'good' is unaffected by it.
    assert wait_for(lambda: sup.passes >= 1)
    events = [e["msg"] for e in store.project("bad")["events"]]
    assert any("error: pass failed: RuntimeError: nomad-pm unreachable" in m for m in events)
    assert store.project("good")["state"] == "idle"

    # The loop kept going, and the next pass recovered the project.
    assert wait_for(lambda: sup.passes >= 3)
    assert t.is_alive() and sup.fatal is None
    assert store.project("bad")["state"] == "idle"
    assert sched.calls["bad"] >= 2
    sup.halt()
    t.join(2)


def test_repeated_loop_failures_give_up_so_systemd_sees_it(store, monkeypatch):
    sup = sv.Supervisor(store, pass_secs=0.01)
    sup.store.set_autopilot("a", True)
    sup.start_project("a")

    def broken():
        raise OSError("state dir is read-only")
    monkeypatch.setattr(sup, "run_all", broken)
    t = threading.Thread(target=sup.loop, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive()
    assert sup.fatal and "read-only" in sup.fatal
    assert sup.health()["ok"] is False


# ---- the pass plumbing later tasks rely on ----------------------------------------------------

class Fake(sv.Scheduler):
    def __init__(self, tasks):
        self.tasks = tasks
        self.done = []
        self.slots_seen = []

    def collect(self, ctx):
        out, self.done = self.done, []
        return out

    def select_tasks(self, ctx):
        live = {w["task"] for w in ctx.project["workers"]}
        return [{"task": t} for t in self.tasks if t not in live]

    def start_workers(self, ctx, tasks):
        self.slots_seen.append(ctx.slots)
        return [{"task": t["task"]} for t in tasks]


def test_caps_limit_new_workers_and_collect_frees_slots(store):
    sched = Fake([1, 2, 3, 4, 5])
    sup = sv.Supervisor(store, sched)
    sup.store.set_autopilot("a", True)
    sup.start_project("a")
    sup.run_all()
    p = store.project("a")
    assert [w["task"] for w in p["workers"]] == [1, 2, 3]       # per-project cap 3
    assert (p["state"], p["detail"]) == ("running", "3 worker(s): #1, #2, #3")

    sched.done = [{"task": 2, "outcome": "merge", "cost_usd": 0.5}]
    sched.tasks = [1, 3, 4, 5]
    sup.run_all()
    assert [w["task"] for w in store.project("a")["workers"]] == [1, 3, 4]
    assert store.spent_today() == 0.5


def test_global_cap_is_shared_between_projects(store):
    store.update_config({"global_cap": 4})
    sup = sv.Supervisor(store, Fake([1, 2, 3]))
    sup.store.set_autopilot("a", True)
    sup.start_project("a")
    sup.store.set_autopilot("b", True)
    sup.start_project("b")
    sup.run_all()
    counts = [len(store.project(p)["workers"]) for p in ("a", "b")]
    assert counts == [3, 1]


def test_budget_cap_pauses_new_work(store):
    store.update_config({"daily_cap_usd": 1.0})
    store.add_spend(1.0)
    sched = Fake([1])
    sup = sv.Supervisor(store, sched)
    sup.store.set_autopilot("a", True)
    sup.start_project("a")
    sup.run_all()
    p = store.project("a")
    assert p["state"] == "budget_paused" and p["workers"] == []

    store.update_config({"budget_mode": "unlimited"})             # takes effect next pass
    sup.run_all()
    assert store.project("a")["state"] == "running"


def test_disabled_projects_are_not_passed(store):
    sched = Flaky()
    sup = sv.Supervisor(store, sched)
    sup.store.set_autopilot("a", True)
    sup.start_project("a")
    sup.stop_project("a", "finish")
    sup.run_all()
    assert sched.calls == {}


# ---- wiring: merge queue, budget, alerts (#868, #871, #880, #878) ------------------------------

class Merging(Fake):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.merges = []

    def merge(self, ctx, finished):
        self.merges.append(list(finished))


def test_merge_is_called_every_pass_so_a_restarted_queue_drains(store):
    sched = Merging([])
    sup = sv.Supervisor(store, sched)
    sup.store.set_autopilot("a", True)
    sup.start_project("a")
    sup.run_all()
    sup.run_all()
    assert sched.merges == [[], []]
    sched.done = [{"task": 7, "outcome": "merge"}]
    sup.run_all()
    assert sched.merges[-1] == [{"task": 7, "outcome": "merge"}]


class Recorder:
    def __init__(self):
        self.passes, self.states = [], []

    def on_pass(self, pid, finished=(), started=(), state=None, detail=""):
        self.passes.append((pid, [f["task"] for f in finished], [w["task"] for w in started], state))

    def project_state(self, pid, state, detail=""):
        self.states.append((pid, state))


def test_alerter_sees_every_pass_and_pass_failures(store):
    rec = Recorder()
    sched = Fake([1])
    sup = sv.Supervisor(store, sched, alerter=rec)
    sup.store.set_autopilot("a", True)
    sup.start_project("a")
    sup.run_all()
    sched.done = [{"task": 1, "outcome": "failed"}]
    sched.tasks = []
    sup.run_all()
    assert rec.passes == [("a", [], [1], "running"), ("a", [1], [], "idle")]

    sup2 = sv.Supervisor(store, Flaky(), alerter=rec)
    sup2.store.set_autopilot("bad", True)
    sup2.start_project("bad")
    sup2.run_all()
    assert ("bad", "error") in rec.states


def test_budget_object_books_spend_and_pauses(store, tmp_path):
    import budget as bg
    b = bg.Budget(store, runs_dir=str(tmp_path / "runs"))
    store.update_config({"daily_cap_usd": 1.0})
    sched = Fake([1, 2])
    sup = sv.Supervisor(store, sched, budget=b)
    sup.store.set_autopilot("a", True)
    sup.start_project("a")
    sup.run_all()
    sched.done = [{"task": 1, "outcome": "merge", "cost_usd": 1.5}]
    sched.tasks = [2, 3]
    sup.run_all()
    p = store.project("a")
    assert p["state"] == "budget_paused"
    assert [w["task"] for w in p["workers"]] == [2]              # running workers are untouched
    assert sup.status()["budget"]["spent_usd"] == 1.5
    assert sup.status()["budget"]["paused_projects"] == ["a"]


def test_milestone_alerts_go_through_the_alerter_once_per_text():
    sent = []

    class A:
        def notify(self, pid, key, title, body=""):
            sent.append((pid, key, title))

    class Ctx:
        project_id = "a"

    alert = sv.milestone_alert(A())
    alert(Ctx(), "feature_complete", "Feature #9 done")
    alert(Ctx(), "feature_complete", "Feature #9 done")
    assert sent[0][0] == "a" and sent[0][1].startswith("feature_complete:a:")
    assert sent[0][1] == sent[1][1]          # same text, same key: the Alerter's dedup drops it


def test_status_names_the_runnable_projects_and_each_ones_board(store):
    """#883: which rows get a Run button, and which board row a project's status line sits on."""
    class S(sv.Scheduler):
        def runnable(self):
            return {"runematch": "runematch", "civis-poc": "civis"}

        def board_of(self, pid):
            return {"old": "old-board"}.get(pid, pid)
    store.set_autopilot("old", True)
    store.start("old")
    out = sv.Supervisor(store, S()).status()
    assert out["runnable"] == ["civis-poc", "runematch"]
    assert out["boards"] == {"old": "old-board", "runematch": "runematch", "civis-poc": "civis"}
    assert "runnable_error" not in out


def test_status_survives_an_unreadable_registry(store):
    class S(sv.Scheduler):
        def runnable(self):
            raise ValueError("projects.yaml: bad entry")
    out = sv.Supervisor(store, S()).status()
    assert out["ok"] and out["runnable"] == [] and "bad entry" in out["runnable_error"]


def test_the_base_scheduler_offers_no_run_button(store):
    out = sv.Supervisor(store).status()
    assert out["runnable"] == [] and out["boards"] == {}


# ---- the switch and run mode "once" (#890) ----------------------------------------------------

class OnceSched(sv.Scheduler):
    """Ready tasks 1, 2, 3 in priority order. `finish` is what the next collect reports;
    `merging` the task ids the merge queue still holds; `results` its final results."""

    def __init__(self, tasks=(1, 2, 3)):
        self.tasks = [{"task": t} for t in tasks]
        self.started, self.finish, self.merging, self.results = [], [], set(), {}

    def select_tasks(self, ctx):
        held = {str(w.get("task")) for w in ctx.project["workers"]}
        return [t for t in self.tasks if str(t["task"]) not in held]

    def start_workers(self, ctx, tasks):
        self.started += [t["task"] for t in tasks]
        return [{"task": t["task"]} for t in tasks]

    def collect(self, ctx):
        out, self.finish = self.finish, []
        return out

    def merge_result(self, ctx, tid):
        return None if tid in self.merging else self.results.get(tid, "merged")


def test_the_switch_is_off_for_a_new_project_and_set_over_http(served):
    sup, base = served
    assert sup.store.project("runematch")["autopilot"] is False
    code, out = call(base, "POST", "/projects/runematch/autopilot", {"on": True})
    assert code == 200 and out["project"]["autopilot"] is True
    assert out["project"]["enabled"] is False            # the switch is not Run
    assert call(base, "GET", "/status")[1]["projects"]["runematch"]["autopilot"] is True
    for bad in ({}, {"on": "yes"}, {"on": 1}, {"on": None}):
        assert call(base, "POST", "/projects/runematch/autopilot", bad)[0] == 400
    assert call(base, "POST", "/projects/runematch/autopilot", {"on": False}, token=None)[0] == 401
    assert call(base, "POST", "/projects/..%2F/autopilot", {"on": True})[0] == 404
    assert call(base, "POST", "/projects/runematch/autopilot/x", {"on": True})[0] == 404


def test_the_switch_survives_a_restart(tmp_path):
    path = str(tmp_path / "state.json")
    st.StateStore(path).set_autopilot("a", True)
    again = st.StateStore(path)
    assert again.project("a")["autopilot"] is True
    again.set_autopilot("a", False)
    assert st.StateStore(path).project("a")["autopilot"] is False


def test_a_state_file_from_before_the_switch_keeps_running_projects_on(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema": 1, "projects": {
        "running": {"enabled": True, "state": "running", "workers": [{"task": 9}]},
        "stopped": {"enabled": False, "state": "off"}}}))
    s = st.StateStore(str(path))
    assert s.project("running")["autopilot"] is True and s.project("running")["run_mode"] == "continuous"
    assert s.project("stopped")["autopilot"] is False
    assert s.project("never-seen")["autopilot"] is False


def test_switch_on_runs_continuously_as_before(store):
    sch = OnceSched()
    sup = sv.Supervisor(store, sch)
    store.set_autopilot("a", True)
    sup.start_project("a")
    sup.run_all()
    assert store.project("a")["run_mode"] == "continuous" and sch.started == [1, 2, 3]
    sch.finish = [{"task": 1, "outcome": "failed"}]
    sup.run_all()
    assert store.project("a")["enabled"] is True


def test_switch_off_starts_exactly_the_top_task_and_nothing_more(store):
    sch = OnceSched()
    sup = sv.Supervisor(store, sch)
    sup.start_project("a")
    p = store.project("a")
    assert p["run_mode"] == "once" and "one task" in p["detail"]
    for _ in range(3):
        sup.run_all()
    p = store.project("a")
    assert sch.started == [1] and [w["task"] for w in p["workers"]] == [1]
    assert p["state"] == "running" and p["detail"] == "one task #1: 1 worker"
    assert p["once"] == {"task": "1", "phase": "working"}


@pytest.mark.parametrize("outcome", ["failed", "needs_operator", "resume", "error"])
def test_a_non_merge_outcome_ends_the_run(store, outcome):
    sch = OnceSched()
    sup = sv.Supervisor(store, sch)
    sup.start_project("a")
    sup.run_all()
    sch.finish = [{"task": 1, "outcome": outcome}]
    sup.run_all()
    p = store.project("a")
    assert p["enabled"] is False and p["state"] == "off"
    assert p["detail"] == f"one task done: #1 {outcome}"
    assert p["once"] is None and p["workers"] == [] and sch.started == [1]
    sup.run_all()                                        # off: nothing more starts
    assert sch.started == [1]


def test_a_merge_waits_for_the_queue_then_ends_the_run(store):
    sch = OnceSched()
    sup = sv.Supervisor(store, sch)
    sup.start_project("a")
    sup.run_all()
    sch.merging.add("1")
    sch.finish = [{"task": 1, "outcome": "merge"}]
    for _ in range(3):
        sup.run_all()
        p = store.project("a")
        assert p["enabled"] is True and p["state"] == "running"
        assert p["detail"] == "one task #1: in the merge queue" and p["workers"] == []
    assert sch.started == [1]                            # tasks 2 and 3 qualify; none started
    sch.merging.clear()
    sup.run_all()
    p = store.project("a")
    assert (p["enabled"], p["state"], p["detail"]) == (False, "off", "one task done: #1 merged")
    assert sch.started == [1]


def test_the_queue_result_is_the_outcome_when_it_did_not_merge(store):
    sch = OnceSched()
    sch.results["1"] = "blocked"
    sup = sv.Supervisor(store, sch)
    sup.start_project("a")
    sup.run_all()
    sch.finish = [{"task": 1, "outcome": "merge"}]
    sup.run_all()
    assert store.project("a")["detail"] == "one task done: #1 blocked"


def test_a_restart_keeps_the_mode_and_the_task(tmp_path):
    path = str(tmp_path / "state.json")
    sch = OnceSched()
    sup = sv.Supervisor(st.StateStore(path), sch)
    sup.start_project("a")
    sup.run_all()
    # restart while the worker runs: the same run, nothing new starts
    sch2 = OnceSched()
    sup2 = sv.Supervisor(st.StateStore(path), sch2)
    sup2.recover()
    sup2.run_all()
    p = sup2.store.project("a")
    assert p["run_mode"] == "once" and p["once"]["task"] == "1" and sch2.started == []
    # the worker ends `merge`; restart again while the merge is pending
    sch2.merging.add("1")
    sch2.finish = [{"task": 1, "outcome": "merge"}]
    sup2.run_all()
    sch3 = OnceSched()
    sch3.merging.add("1")
    sup3 = sv.Supervisor(st.StateStore(path), sch3)
    sup3.recover()
    sup3.run_all()
    p = sup3.store.project("a")
    assert p["enabled"] and p["once"] == {"task": "1", "phase": "merging"} and sch3.started == []
    sch3.merging.clear()
    sup3.run_all()
    p = sup3.store.project("a")
    assert (p["enabled"], p["detail"]) == (False, "one task done: #1 merged")
    assert sch3.started == []


def test_nothing_qualifies_goes_straight_to_off_with_the_reason(store):
    sch = OnceSched(tasks=())
    sup = sv.Supervisor(store, sch)
    sup.start_project("a")
    sup.run_all()
    p = store.project("a")
    assert (p["enabled"], p["state"]) == (False, "off")
    assert p["detail"] == "one task: nothing qualifies (nothing to do)"


def test_a_full_cap_or_budget_waits_and_then_starts_one(store):
    sch = OnceSched()
    sup = sv.Supervisor(store, sch)
    store.update_config({"global_cap": 0})
    sup.start_project("a")
    sup.run_all()
    p = store.project("a")
    assert p["enabled"] and p["state"] == "waiting" and "#1 ready" in p["detail"] and sch.started == []
    store.update_config({"global_cap": 6, "daily_cap_usd": 0.0})
    sup.run_all()
    p = store.project("a")
    assert p["enabled"] and p["state"] == "budget_paused" and sch.started == []
    store.update_config({"budget_mode": "unlimited"})
    sup.run_all()
    sup.run_all()
    assert sch.started == [1]


def test_flipping_the_switch_changes_the_next_run_not_this_one(store):
    sch = OnceSched()
    sup = sv.Supervisor(store, sch)
    sup.start_project("a")
    sup.run_all()
    store.set_autopilot("a", True)
    sup.start_project("a")                               # Run again while on: no change
    sup.run_all()
    assert store.project("a")["run_mode"] == "once" and sch.started == [1]
    sup.stop_project("a", "finish")
    assert store.project("a")["once"] is None
    sch.finish = [{"task": 1, "outcome": "failed"}]
    sup.run_all()                                        # drained while off
    sup.start_project("a")
    sup.run_all()
    assert store.project("a")["run_mode"] == "continuous" and sch.started == [1, 1, 2, 3]


# ---- GET /runs: the worker runs the cockpit's runs table shows (#955) ----------------------------

def _run(tmp_path, project, task, started, collected=None, deadline=None, result=None, **extra):
    """Write a worker `.run.json` the way worker.py does, with an optional stream-json log."""
    d = tmp_path / "runs" / project
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(started))
    log = d / f"{task}-{stamp}.jsonl"
    lines = [{"type": "system", "subtype": "init"}, {"type": "assistant", "message": {}}]
    if result is not None:
        lines.append({"type": "result", "subtype": "success", "total_cost_usd": 0.41234,
                      "result": result})
    log.write_text("".join(json.dumps(x) + "\n" for x in lines))
    rec = {"project": project, "task": task, "branch": f"feat/{task}-x", "log": str(log),
           "started": started, "deadline": deadline if deadline is not None else started + 3600}
    if collected:
        rec.update(collected=collected, collected_at=started + 600)
    rec.update(extra)
    (d / f"{task}-{stamp}.run.json").write_text(json.dumps(rec))


def test_runs_are_newest_first_across_projects_and_limited(tmp_path):
    now = time.time()
    _run(tmp_path, "alpha", "11", now - 9000, collected="merge", result="done")
    _run(tmp_path, "beta", "22", now - 5000, collected="failed", result="broke")
    _run(tmp_path, "alpha", "33", now - 100)
    runs = sv.recent_runs(str(tmp_path / "runs"), now=now)
    assert [(r["project"], r["task"]) for r in runs] == [("alpha", "33"), ("beta", "22"),
                                                          ("alpha", "11")]
    assert [r["task"] for r in sv.recent_runs(str(tmp_path / "runs"), limit=2, now=now)] == ["33", "22"]


def test_a_run_is_running_until_collected_and_unknown_past_its_deadline(tmp_path):
    now = time.time()
    _run(tmp_path, "p", "1", now - 60)
    _run(tmp_path, "p", "2", now - 7200, deadline=now - 3600)
    _run(tmp_path, "p", "3", now - 9000, collected="needs_operator", result="ask")
    got = {r["task"]: r for r in sv.recent_runs(str(tmp_path / "runs"), now=now)}
    assert got["1"]["outcome"] == "running" and got["1"]["finished"] is None
    assert got["2"]["outcome"] == "unknown"
    assert got["3"]["outcome"] == "needs_operator" and got["3"]["finished"] == now - 9000 + 600


def test_the_summary_is_the_last_paragraph_without_the_marker(tmp_path):
    now = time.time()
    text = "Did a thing.\n\nMerged main, ran the gate: 214 passed.\n\nautopilot: result ready to merge"
    _run(tmp_path, "p", "7", now - 60, collected="merge", result=text)
    [r] = sv.recent_runs(str(tmp_path / "runs"), now=now)
    assert r["summary"] == "Merged main, ran the gate: 214 passed."
    assert r["cost_usd"] == 0.4123
    assert r["branch"] == "feat/7-x" and r["id"].startswith("7-")


def test_a_long_summary_is_cut(tmp_path):
    now = time.time()
    _run(tmp_path, "p", "8", now - 60, collected="merge", result="word " * 200)
    [r] = sv.recent_runs(str(tmp_path / "runs"), now=now)
    assert len(r["summary"]) == sv.SUMMARY_CHARS and r["summary"].endswith("…")


def test_bad_records_are_skipped_and_a_missing_log_is_no_summary(tmp_path):
    now = time.time()
    _run(tmp_path, "p", "9", now - 60, collected="merge", log=str(tmp_path / "gone.jsonl"))
    d = tmp_path / "runs" / "p"
    (d / "broken.run.json").write_text("{not json")
    (d / "notask.run.json").write_text(json.dumps({"started": now}))
    (d / "nostart.run.json").write_text(json.dumps({"task": "5", "started": "yesterday"}))
    [r] = sv.recent_runs(str(tmp_path / "runs"), now=now)
    assert r["task"] == "9" and r["summary"] == "" and r["cost_usd"] is None


def test_no_runs_dir_is_an_empty_list(tmp_path):
    assert sv.recent_runs(str(tmp_path / "nothing")) == []


def test_runs_route_is_open_and_reads_beside_the_state_file(served, tmp_path):
    _, base = served
    now = time.time()
    for i in range(5):
        _run(tmp_path, "p", str(i), now - 1000 + i, collected="merge", result="ok")
    code, body = call(base, "GET", "/runs", token=None)
    assert code == 200 and [r["task"] for r in body["runs"]] == ["4", "3", "2", "1", "0"]
    code, body = call(base, "GET", "/runs?limit=2", token=None)
    assert code == 200 and len(body["runs"]) == 2
    assert call(base, "GET", "/runs?limit=lots", token=None)[0] == 400
    assert call(base, "GET", "/runs?limit=0", token=None)[0] == 200
