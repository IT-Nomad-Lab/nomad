"""Tests for criteria drafts (#889, PRD 5.2): New tasks without acceptance criteria get one
read-only drafting run, posted by the supervisor as `autopilot: criteria draft`. A fake `claude`
script stands in for every run; no real claude is launched.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest autopilot -q
"""
import json
import os
import sys
import time

import pytest

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, NOMAD)

from autopilot import budget as bg  # noqa: E402
from autopilot import criteria as cr  # noqa: E402
from autopilot import permissions as P  # noqa: E402
from autopilot import state as st  # noqa: E402
from autopilot import supervisor as sv  # noqa: E402
from autopilot import tasks as tk  # noqa: E402
from autopilot import worker as wk  # noqa: E402
from autopilot.test_scheduler import FakePM, Rig, fake, make_project, wp  # noqa: E402,F401

DRAFT_FAKE = r'''#!{python}
import json, os, re, sys, time
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
tid = re.search(r"Task #(\d+)", prompt).group(1)
out = os.environ.get("FAKE_DRAFT_OUT")
if out:
    with open(os.path.join(out, "draft-" + tid + ".json"), "w") as f:
        json.dump({{"argv": argv, "cwd": os.getcwd()}}, f)
mode = os.environ.get("FAKE_DRAFT_" + tid) or os.environ.get("FAKE_DRAFT", "criteria")
def emit(o):
    print(json.dumps(o), flush=True)
emit({{"type": "system", "subtype": "init"}})
if mode == "sleep":
    time.sleep(600)
if mode == "crash":
    sys.exit(2)
text = {{
    "criteria": "CRITERIA\n- the page lists every project\n- a stopped project shows `off`",
    "reserved": "RESERVED: the task names a new product; naming is reserved for the operator.",
    "garbage": "I think it is fine.",
}}[mode]
emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": text}}]}}}})
emit({{"type": "result", "subtype": "success", "is_error": False, "result": text,
      "total_cost_usd": 0.05, "permission_denials": []}})
'''


class MovingPM(FakePM):
    """FakePM plus `transition`, as the live PMClient has."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.moves = []

    def transition(self, i, status):
        self.moves.append((i, status))
        self.set_status(i, status)


@pytest.fixture
def draft_fake(tmp_path, monkeypatch):
    f = tmp_path / "claude-draft"
    f.write_text(DRAFT_FAKE.format(python=sys.executable))
    f.chmod(0o755)
    out = tmp_path / "draft-out"
    out.mkdir()
    monkeypatch.setenv("FAKE_DRAFT_OUT", str(out))
    monkeypatch.setenv("FAKE_DRAFT", "criteria")
    return str(f)


class Ctx:
    def __init__(self, pid="proj", slots=1):
        self.project_id = pid
        self.slots = slots
        self.events = []

    def event(self, kind, msg):
        self.events.append((kind, msg))


def task(i=103, status="New", description="Make the PROJECTS panel list projects.", draft=None):
    return tk.Task(id=i, subject=f"task {i}", status=status, type="Task", lane="dev",
                   description=description, draft=draft)


def wait_collect(drafter, ctx, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        done = drafter.collect(ctx)
        if done:
            return done
        time.sleep(0.05)
    raise AssertionError("draft never finished")


# ---- reading drafts from comments (tasks.py) ------------------------------------------------

DRAFTED = cr.comment_for("drafted", ["it lists projects", "stopped shows off"])


def test_criteria_draft_states():
    assert tk.criteria_draft([]) is None
    assert tk.criteria_draft(["autopilot: started x", "just a note"]) is None
    assert tk.criteria_draft(["x", DRAFTED])[0] == "drafted"
    assert tk.criteria_draft([cr.comment_for("reserved", "naming.")])[0] == "reserved"
    assert tk.criteria_draft([cr.comment_for("failed", "no answer")])[0] == "failed"
    assert tk.criteria_draft(["autopilot: criteria draft\n\nnothing usable"])[0] == "failed"
    assert DRAFTED.startswith(tk.DRAFT_MARK) and tk.has_acceptance(DRAFTED)


def test_classify_new_tasks_by_draft():
    assert tk.classify(task()) == ("criteria", "New without acceptance criteria")
    assert tk.classify(task(draft=("drafted", DRAFTED))) is None
    kind, reason = tk.classify(task(draft=("reserved", "")))
    assert kind == "operator" and "reserved decision" in reason
    kind, reason = tk.classify(task(draft=("failed", "")))
    assert kind == "operator" and "draft failed" in reason
    # criteria in the description win over any draft; the worker is told only about a draft
    own = task(description="**Acceptance criteria:** it works.", draft=("failed", ""))
    assert tk.classify(own) is None and own.as_dict()["criteria_draft"] is False
    assert task(draft=("drafted", DRAFTED)).as_dict()["criteria_draft"] is True


def test_fetch_reads_drafts_for_new_tasks_only(tmp_path):
    pm = FakePM(tmp_path / "b", [wp(1, "New"), wp(2, "Ready")])
    pm.notes = {1: [DRAFTED], 2: [DRAFTED]}
    ts = {t.id: t for t in tk.fetch(pm, "b")}
    assert ts[1].draft[0] == "drafted" and ts[2].draft is None
    sel = tk.select(list(ts.values()))
    assert [t.id for t in sel.qualified] == [1, 2]


def test_worker_prompt_points_at_the_draft(tmp_path):
    p = make_project(tmp_path)
    w = wk.Worktree(str(tmp_path / "wt"), "feat/1-x", "main", True, True, False)
    prompt = wk.build_prompt(p, {"task": 1, "status": "New", "promote": True,
                                 "criteria_draft": True}, w, time.time() + 60)
    assert "`autopilot: criteria draft`" in prompt
    plain = wk.build_prompt(p, {"task": 1, "status": "Ready"}, w, time.time() + 60)
    assert "criteria draft" not in plain


def test_worker_prompt_carries_the_operators_answer(tmp_path):
    p = make_project(tmp_path)
    w = wk.Worktree(str(tmp_path / "wt"), "feat/1-x", "main", False, False, False)
    ok = wk.build_prompt(p, {"task": 1, "status": "Approved", "decision": "approved",
                             "answer": "(a), keep the HUD"}, w, time.time() + 60)
    assert "they approved it" in ok and "(a), keep the HUD" in ok and "In Progress" in ok
    assert "autopilot: resume` comment" not in ok
    bare = wk.build_prompt(p, {"task": 1, "status": "Approved", "decision": "approved",
                               "answer": ""}, w, time.time() + 60)
    assert "go ahead as you proposed" in bare
    no = wk.build_prompt(p, {"task": 1, "status": "Changes requested",
                             "decision": "changes requested", "answer": ""}, w, time.time() + 60)
    assert "requested changes" in no and "`needs operator`" in no
    plain = wk.build_prompt(p, {"task": 1, "status": "In Progress"}, w, time.time() + 60)
    assert "operator answered" not in plain and "autopilot: resume` comment" in plain
    multi = "really contain\n`Bash(git restore *)`\n\nthen (2)"      # #982
    assert multi in wk.build_prompt(p, {"task": 1, "status": "Approved", "decision": "approved",
                                        "answer": multi}, w, time.time() + 60)


# ---- the answer ------------------------------------------------------------------------------

def test_parse_answer():
    assert cr.parse_answer("CRITERIA\n- a\n* b\n1. c") == ("drafted", ["a", "b", "c"])
    assert cr.parse_answer("**CRITERIA**\n\n- a") == ("drafted", ["a"])
    assert cr.parse_answer("RESERVED: naming the product.") == ("reserved", "naming the product.")
    assert cr.parse_answer("")[0] == "failed"
    assert cr.parse_answer("CRITERIA\n")[0] == "failed"
    assert cr.parse_answer("sure, looks good")[0] == "failed"
    many = "CRITERIA\n" + "\n".join(f"- c{i}" for i in range(20))
    assert len(cr.parse_answer(many)[1]) == cr.MAX_CRITERIA


def test_comment_is_a_delegated_decision():
    assert "Delegated decision:" in DRAFTED and "- it lists projects" in DRAFTED
    r = cr.comment_for("reserved", "naming.")
    assert r.startswith(tk.DRAFT_MARK + " needs operator") and "Awaiting approval" in r


# ---- permissions ------------------------------------------------------------------------------

def test_draft_settings_are_read_only(tmp_path):
    path = P.write_criteria(str(tmp_path))
    s = json.load(open(path))["permissions"]
    assert s["defaultMode"] == "dontAsk"
    assert all(r.startswith("mcp__nomad-pm__") for r in s["allow"])
    assert "mcp__nomad-pm__comment" not in s["allow"] and "mcp__nomad-pm" not in s["allow"]
    for tool in ("Bash", "Edit", "Write", "mcp__nomad-pm__comment", "mcp__nomad-pm__transition",
                 "mcp__nomad-pm__update_fields"):
        assert tool in s["deny"]


# ---- the drafter ------------------------------------------------------------------------------

def make_drafter(tmp_path, draft_fake, pm=None, **kw):
    pm = pm or MovingPM(tmp_path / "board", [wp(103, "New")])
    return cr.CriteriaDrafter(pm, claude=draft_fake, state_dir=str(tmp_path / "state"), **kw), pm


def sel_of(*tasks):
    return tk.select(list(tasks))


def test_draft_launch_args_and_post(tmp_path, draft_fake):
    d, pm = make_drafter(tmp_path, draft_fake)
    ctx = Ctx()
    assert d.draft(ctx, sel_of(task())) == 103
    assert d.pending("proj") == [103]
    assert d.draft(ctx, sel_of(task(), task(104))) is None      # one live draft per project
    assert wait_collect(d, ctx) == [(103, "drafted")]
    rec = json.load(open(tmp_path / "draft-out" / "draft-103.json"))
    argv = rec["argv"]
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv                        # nomad-pm, and nothing else
    assert argv[argv.index("--mcp-config") + 1].endswith("/settings/mcp.json")
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--settings") + 1].endswith("criteria-draft.json")
    assert argv[argv.index("--max-turns") + 1] == str(cr.DRAFT_TURNS)
    assert "--model" in argv and rec["cwd"] == str(tmp_path / "state" / "criteria")
    [note] = pm.notes[103]
    assert tk.criteria_draft([note])[0] == "drafted" and "- a stopped project shows `off`" in note
    assert pm.moves == [] and ("criteria", "task 103: criteria draft drafted") in ctx.events
    # the log sits with the workers' logs
    assert d.logs("proj", 103)[0].startswith(str(tmp_path / "state" / "runs" / "proj"))


def test_reserved_draft_moves_to_awaiting_approval(tmp_path, draft_fake, monkeypatch):
    monkeypatch.setenv("FAKE_DRAFT", "reserved")
    d, pm = make_drafter(tmp_path, draft_fake)
    ctx = Ctx()
    d.draft(ctx, sel_of(task()))
    assert wait_collect(d, ctx) == [(103, "reserved")]
    assert pm.moves == [(103, "Awaiting approval")]
    assert tk.criteria_draft(pm.notes[103])[0] == "reserved"


@pytest.mark.parametrize("mode", ["garbage", "crash"])
def test_unusable_draft_is_failed(tmp_path, draft_fake, monkeypatch, mode):
    monkeypatch.setenv("FAKE_DRAFT", mode)
    d, pm = make_drafter(tmp_path, draft_fake)
    ctx = Ctx()
    d.draft(ctx, sel_of(task()))
    assert wait_collect(d, ctx) == [(103, "failed")]
    assert tk.criteria_draft(pm.notes[103])[0] == "failed" and pm.moves == []


def test_time_limit_kills_and_fails(tmp_path, draft_fake, monkeypatch):
    monkeypatch.setenv("FAKE_DRAFT", "sleep")
    d, pm = make_drafter(tmp_path, draft_fake, run_secs=0.2)
    ctx = Ctx()
    d.draft(ctx, sel_of(task()))
    time.sleep(0.3)
    assert d.collect(ctx) == [(103, "failed")]
    assert "time limit" in pm.notes[103][0]


def test_never_redrafts(tmp_path, draft_fake):
    d, pm = make_drafter(tmp_path, draft_fake)
    ctx = Ctx()
    # a task with a draft comment is not kind `criteria` any more, but even a stale Selection
    # holding it with a draft is skipped
    stale = tk.Selection([], [tk.Skipped(task(draft=("failed", "")), "criteria", "")])
    assert d.draft(ctx, stale) is None
    d.draft(ctx, sel_of(task()))
    wait_collect(d, ctx)
    # the comment was lost (posting failed, or the board lags): the log on disk still stops a
    # second run, and the answer is posted from it instead
    pm.notes.clear()
    assert d.draft(ctx, sel_of(task())) is None
    assert tk.criteria_draft(pm.notes[103])[0] == "drafted"
    assert len(d.logs("proj", 103)) == 1


def test_a_draft_from_before_a_restart_is_posted_not_relaunched(tmp_path, draft_fake, monkeypatch):
    d, pm = make_drafter(tmp_path, draft_fake)
    d.draft(Ctx(), sel_of(task()))
    d.runs[("proj", 103)].proc.wait(timeout=10)
    d2, _ = make_drafter(tmp_path, draft_fake, pm=pm)             # the restarted supervisor's
    assert d2.draft(Ctx(), sel_of(task())) is None and d2.pending("proj") == []
    assert tk.criteria_draft(pm.notes[103])[0] == "drafted"
    # a log without a result, still young, may be a run still going: left alone
    monkeypatch.setenv("FAKE_DRAFT", "crash")
    young = os.path.join(d.state_dir, "runs", "proj", "criteria-105-x.jsonl")
    open(young, "w").write("")
    assert d2.draft(Ctx(), sel_of(task(105))) is None and 105 not in pm.notes
    os.utime(young, (time.time() - 10_000, time.time() - 10_000))
    d2.draft(Ctx(), sel_of(task(105)))
    assert tk.criteria_draft(pm.notes[105])[0] == "failed"


def test_budget_counts_draft_runs(tmp_path, draft_fake):
    d, pm = make_drafter(tmp_path, draft_fake)
    ctx = Ctx()
    d.draft(ctx, sel_of(task()))
    wait_collect(d, ctx)
    store = st.StateStore(str(tmp_path / "state" / "state.json"))
    b = bg.Budget(store)
    assert b.reconcile() == pytest.approx(0.05)
    assert b.status()["by_project"] == {"proj": pytest.approx(0.05)}


# ---- through the supervisor -------------------------------------------------------------------

def test_scheduler_drafts_then_the_task_qualifies(tmp_path, fake, draft_fake):
    p = make_project(tmp_path)
    pm = MovingPM(tmp_path / "board", [wp(103, "New", "Make the PROJECTS panel list projects.")])
    rig = Rig(tmp_path, fake, {"proj": p}, pm)
    rig.sched.drafter = cr.CriteriaDrafter(pm, claude=draft_fake, state_dir=str(tmp_path / "state"))
    try:
        rig.sup.store.set_autopilot("proj", True)
        rig.sup.start_project("proj")
        rig.sup.run_all()
        pr = rig.project()
        assert pr["state"] == "idle" and "#103 (drafting)" in pr["detail"]
        assert pr["workers"] == []
        pr = rig.until(lambda pr: any(w["task"] == "103" for w in pr["workers"])
                       or "103" in rig.merged)
        assert tk.criteria_draft(pm.notes[103])[0] == "drafted"
        rig.until(lambda pr: pr["state"] == "idle" and "103" in rig.merged)
        prompt = json.load(open(tmp_path / "fake-out" / "103.argv.json"))["argv"][1]
        assert "`autopilot: criteria draft`" in prompt
        assert len(cr.CriteriaDrafter.logs(rig.sched.drafter, "proj", 103)) == 1
    finally:
        rig.kill_all()


def test_no_draft_while_budget_paused(tmp_path, fake, draft_fake):
    p = make_project(tmp_path)
    pm = MovingPM(tmp_path / "board", [wp(103, "New")])
    rig = Rig(tmp_path, fake, {"proj": p}, pm)
    rig.sched.drafter = cr.CriteriaDrafter(pm, claude=draft_fake, state_dir=str(tmp_path / "state"))
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.store.update_config({"budget_mode": "cap", "daily_cap_usd": 0.0})
    rig.sup.run_all()
    assert rig.sched.drafter.pending("proj") == [] and rig.sched.drafter.logs("proj", 103) == []


def test_without_a_drafter_new_tasks_are_only_reported(tmp_path, fake):
    p = make_project(tmp_path)
    pm = MovingPM(tmp_path / "board", [wp(103, "New")])
    rig = Rig(tmp_path, fake, {"proj": p}, pm)
    rig.sup.store.set_autopilot("proj", True)
    rig.sup.start_project("proj")
    rig.sup.run_all()
    assert rig.project()["detail"] == "nothing to do (1 New without acceptance criteria: #103)"
    assert pm.notes == {}
