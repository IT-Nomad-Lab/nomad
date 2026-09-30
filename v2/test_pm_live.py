"""PM-01 P3 · the engine against a real OpenProject, end to end.

The offline tests (test_pm_engine.py) prove the engine's logic over a fake board. This proves the
board: the real client, the real workflow and the real per-lane tokens. LLM, specialists and the
tool are stubbed and NocoDB is a FakeDB, so only OpenProject is live.

Skipped unless NOMAD_PM_LIVE=1. It writes into the scratch project pm-test-a (override with
NOMAD_PM_PROJECT), so create it first and remove it after:
  ~/nomad-pm/tests/fixture.sh setup
  NOMAD_PM_LIVE=1 python3 -m pytest v2/test_pm_live.py -q
  ~/nomad-pm/tests/fixture.sh teardown
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine as engine_mod  # noqa: E402
from engine import Engine, proposal_hash  # noqa: E402
from pm_openproject import OpenProject, _title, parent_id  # noqa: E402
from test_engine_states import FakeDB  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("NOMAD_PM_LIVE") != "1", reason="set NOMAD_PM_LIVE=1")

LANES = ["comms", "research", "support", "ads", "dev"]
ROUTES = {"Collect sources": "research", "Draft the brief": "research", "Tell the operator": "comms"}
PLAN = {"milestones": [
    {"title": "Gather", "tasks": [
        {"key": "t1", "title": "Collect sources", "lane": "research", "after": []},
        {"key": "t2", "title": "Draft the brief", "lane": "research", "after": ["t1"]}]},
    {"title": "Share", "tasks": [
        {"key": "t3", "title": "Tell the operator", "lane": "comms", "after": ["t2"]}]}],
    "assumptions": ["live test"], "risk": "none"}


class Spec:
    def propose(self, intent, target, run_id):
        return {"action": "send_message", "args": {"to": "operator", "body": intent}}


@pytest.fixture(scope="module")
def world():
    mp = pytest.MonkeyPatch()
    op = OpenProject(project=os.environ.get("NOMAD_PM_PROJECT", "pm-test-a"))
    db = FakeDB()
    e = Engine(db=db, pm="openproject", op=op)
    e.auto_advance = False
    e.specialists = {l: Spec() for l in LANES}
    state = {"tool_ok": True}
    e.tools = {"send_message": lambda **kw: {"ok": state["tool_ok"], "summary": "sent",
                                             "error": "" if state["tool_ok"] else "smtp down (live test)"}}
    mp.setattr(engine_mod.llm, "chat_json", lambda m, system, user: json.loads(json.dumps(PLAN))
               if "Planner" in system else {"lane": ROUTES.get(user.removeprefix("Goal: "), "comms"),
                                            "intent": user.removeprefix("Goal: "), "target": "operator"})
    mp.setattr(engine_mod.memory, "recall", lambda db, q: "")
    plan = e.plan_project("P3 live test", "written by v2/test_pm_live.py")
    yield e, db, op, plan, state
    mp.undo()


def _authors(op, wp_id):
    acts = op.call("GET", f"/work_packages/{wp_id}/activities")["_embedded"]["elements"]
    out = []
    for a in acts:
        if (a.get("comment") or {}).get("raw"):
            # Members cannot read another user's login; the name seed.rb gives is enough.
            out.append(op.call("GET", a["_links"]["user"]["href"])["name"])
    return out


def _approve(e, db, rid):
    return e.resume_run(rid, "approved",
                        proposal_hash(json.loads(db.find("comms", "run_id", rid)["payload"])))


def _ids(op, plan):
    return [t["task_id"] for m in plan["milestones"] for t in m["tasks"]]


def test_plan_is_on_the_board(world):
    e, db, op, plan, _ = world
    f = plan["goal_id"]
    assert _title(op.get(f), "type") == "Feature"
    t1, t2, t3 = _ids(op, plan)
    assert plan["relations"] == 2 and plan["relation_errors"] == []
    for t in (t1, t2, t3):
        wp = op.get(t)
        assert _title(wp, "type") == "Task" and _title(wp, "status") == "Ready"
        assert _title(op.get(parent_id(wp)), "type") == "Epic"
    assert op.lane_of(op.get(t3)) == "comms"
    assert op.open_blockers(t2) == [t1] and op.open_blockers(t3) == [t2]


def test_next_mirrors_the_gate_as_the_lane(world):
    e, db, op, plan, _ = world
    t1 = _ids(op, plan)[0]
    r = e.run_next_task(plan["goal_id"])
    assert r["task_id"] == t1 and r["status"] == "awaiting-approval"
    wp = op.get(t1)
    assert _title(wp, "status") == "Awaiting approval"
    assert wp[op._fields()["Run ID"]] == r["run_id"]
    assert _authors(op, t1) == ["Manager (agent)", "Research (agent)"]
    assert e.run_next_task(plan["goal_id"])["done"] is True     # t2 waits on t1
    world[4]["run1"] = r["run_id"]


def test_executed_is_done_and_unblocks(world):
    e, db, op, plan, state = world
    t1, t2, _ = _ids(op, plan)
    assert _approve(e, db, state["run1"])["status"] == "executed"
    assert _title(op.get(t1), "status") == "Done"
    assert _authors(op, t1)[-1] == "Research (agent)"
    assert op.open_blockers(t2) == []
    assert _title(op.get(parent_id(op.get(t1))), "status") == "In Progress"   # t2 still open


def test_failed_is_blocked_then_retry(world):
    e, db, op, plan, state = world
    t2 = _ids(op, plan)[1]
    r = e.run_next_task(plan["goal_id"])
    assert r["task_id"] == t2
    state["tool_ok"] = False
    assert _approve(e, db, r["run_id"])["status"] == "failed"
    assert _title(op.get(t2), "status") == "Blocked"
    assert e.retry_run(r["run_id"])["status"] == "awaiting-approval"
    assert _title(op.get(t2), "status") == "Awaiting approval"
    state["tool_ok"] = True
    assert _approve(e, db, r["run_id"])["status"] == "executed"
    assert _title(op.get(t2), "status") == "Done"
    assert _title(op.get(parent_id(op.get(t2))), "status") == "Done"          # Gather closed


def test_declined_is_wont_do_and_the_feature_closes(world):
    e, db, op, plan, _ = world
    t3 = _ids(op, plan)[2]
    r = e.run_next_task(plan["goal_id"])
    assert r["task_id"] == t3
    assert e.resume_run(r["run_id"], "rejected")["status"] == "declined"
    assert _title(op.get(t3), "status") == "Won't do"
    assert _authors(op, t3)[-1] == "Comms (agent)"
    assert _title(op.get(plan["goal_id"]), "status") == "Done"
    s = e.project_status(plan["goal_id"])
    assert (s["done"], s["total"]) == (3, 3)
    assert any(g["goal_id"] == plan["goal_id"] for g in e.project_goals())


def test_no_mirror_call_failed(world):
    """Every status above was checked, but a comment or Run ID write could still fail quietly."""
    e, db, *_ = world
    misses = [r for r in db.t["episodic"].values() if r["what"] == "openproject mirror failed"]
    assert misses == [], misses
