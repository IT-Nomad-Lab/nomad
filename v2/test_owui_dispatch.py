"""#1441 · dispatching a work package from Open WebUI (owui_dispatch.py, docs/prd/owui-dispatch.md).

What it pins down:
  - the panel lists open Tasks, Bugs and User stories, never containers or closed items
  - dispatching, against a real Engine, leaves the run awaiting approval with nothing executed
    and nothing sent, and the row then reads `at-gate`
  - a User story can be dispatched; an Epic, a closed item or a bad id is refused
  - the agent state follows the run: at-gate, building, approved, declined, done
  - a failing engine or board answers with an error, never raises

Run: python3 -m pytest v2/test_owui_dispatch.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import owui_dispatch as D  # noqa: E402
from test_pm_engine import FakeOP, _approve, _eng  # noqa: E402


class BoardOP(FakeOP):
    """FakeOP plus the two calls the panel makes that the engine doesn't."""

    def project(self):
        return {"id": self.HOME}

    @staticmethod
    def run_of(wp):
        return wp.get("run_id") or ""


def _setup(monkeypatch):
    e, db, op = _eng(monkeypatch)
    op.__class__ = BoardOP
    sent = []
    e.tools = {"send_message": lambda **kw: sent.append(kw) or {"ok": True, "summary": "sent"}}

    def run_status(rid):
        return (db.find("comms", "run_id", rid) or {}).get("status") or ""
    return e, db, op, sent, run_status


def test_agent_state_follows_the_run():
    assert D.agent_state("", "", False) == ""
    assert D.agent_state("r1", "", False) == "assigned"
    assert D.agent_state("r1", "awaiting-approval", False) == "at-gate"
    assert D.agent_state("r1", "executing", False) == "building"
    assert D.agent_state("r1", "executed", False) == "approved"
    assert D.agent_state("r1", "declined", False) == "declined"
    assert D.agent_state("r1", "failed", False) == "failed"
    assert D.agent_state("r1", "executed", True) == "done"
    assert D.agent_state("", "", True) == ""          # closed by hand, never dispatched


def test_items_lists_open_buildable_work_only(monkeypatch):
    e, db, op, sent, rs = _setup(monkeypatch)
    epic = op.create("Epic", "An epic")["id"]
    task = op.create("Task", "A task", parent=epic)["id"]
    bug = op.create("Bug", "A bug")["id"]
    story = op.create("User story", "A story", parent=epic)["id"]
    done = op.create("Task", "Closed one")["id"]
    op.move(done, "Won't do")
    op.create("Task", "Other board", board=7)
    res = D.items(op, rs, "nomad-work")
    assert res["ok"] and res["board"] == "nomad-work"
    assert [r["id"] for r in res["items"]] == sorted([task, bug, story], reverse=True)
    assert all(r["agent"] == "" for r in res["items"])


def test_dispatch_stops_at_the_gate_and_sends_nothing(monkeypatch):
    """The gate test: the run waits, nothing executes, and the row reads at-gate."""
    e, db, op, sent, rs = _setup(monkeypatch)
    bug = op.create("Bug", "Card-flip flicker")["id"]
    res = D.dispatch(op, e.run_task, rs, f"#{bug}")
    assert res["ok"], res
    assert res["item"]["agent"] == "at-gate"
    assert res["item"]["run_id"] == res["run_id"]
    assert db.find("comms", "run_id", res["run_id"])["status"] == "awaiting-approval"
    assert op.status(bug) == "Awaiting approval"
    assert sent == []
    assert D.status(op, rs, [bug])["items"][0]["agent"] == "at-gate"


def test_a_user_story_can_be_dispatched(monkeypatch):
    e, db, op, sent, rs = _setup(monkeypatch)
    story = op.create("User story", "As the operator I can dispatch")["id"]
    res = D.dispatch(op, e.run_task, rs, story)
    assert res["ok"] and res["item"]["agent"] == "at-gate", res


def test_an_epic_a_closed_item_and_a_bad_id_are_refused(monkeypatch):
    e, db, op, sent, rs = _setup(monkeypatch)
    epic = op.create("Epic", "An epic")["id"]
    closed = op.create("Task", "Closed")["id"]
    op.move(closed, "Won't do")
    for wid in (epic, closed, "abc"):
        res = D.dispatch(op, e.run_task, rs, wid)
        assert res["ok"] is False and res["error"], res
    assert not db.t.get("comms")                     # no run was started for any of them
    assert op.status(epic) == "New" and op.status(closed) == "Won't do"


def test_a_dispatched_item_cannot_be_started_twice(monkeypatch):
    e, db, op, sent, rs = _setup(monkeypatch)
    t = op.create("Task", "Once")["id"]
    assert D.dispatch(op, e.run_task, rs, t)["ok"]
    again = D.dispatch(op, e.run_task, rs, t)
    assert again["ok"] is False and "isn't started again" in again["error"]


def test_the_state_moves_with_approval(monkeypatch):
    e, db, op, sent, rs = _setup(monkeypatch)
    t = op.create("Task", "Ship it")["id"]
    rid = D.dispatch(op, e.run_task, rs, t)["run_id"]
    _approve(e, db, rid)
    row = D.status(op, rs, [t])["items"][0]
    assert row["agent"] == "done" and row["status"] == "Done", row
    assert len(sent) == 1


def test_failures_answer_with_an_error(monkeypatch):
    e, db, op, sent, rs = _setup(monkeypatch)
    t = op.create("Task", "x")["id"]

    def boom(i):
        raise RuntimeError("engine down")
    res = D.dispatch(op, boom, rs, t)
    assert res == {"ok": False, "id": t, "error": "RuntimeError: engine down"}
    op.down = True
    assert D.items(op, rs)["ok"] is False
    assert D.status(op, rs, [t]) == {"ok": True, "items": []}
    json.dumps(res)


def test_the_route_is_behind_the_token_and_general_uses_nomad_work(monkeypatch):
    import pm_openproject
    import server
    from starlette.testclient import TestClient
    e, db, op, sent, rs = _setup(monkeypatch)
    t = op.create("Task", "Listed")["id"]
    boards = []
    monkeypatch.setattr(pm_openproject, "OpenProject", lambda project=None: boards.append(project) or op)
    monkeypatch.setattr(server, "AUTH_MODE", "off")
    monkeypatch.setattr(server.owui, "TOKEN_FILES", ())
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", "t")
    monkeypatch.setattr(server, "db", db)
    client = TestClient(server.app)
    body = {"op": "items", "entry": "general"}
    assert client.post("/owui/dispatch", json=body).status_code == 401
    r = client.post("/owui/dispatch", json=body, headers={"X-Nomad-Token": "t"})
    assert r.status_code == 200 and [i["id"] for i in r.json()["items"]] == [t]
    assert boards == ["nomad-work"]
    r = client.post("/owui/dispatch", json={"op": "approve", "id": t}, headers={"X-Nomad-Token": "t"})
    assert r.json() == {"ok": False, "error": "op must be items, status or dispatch"}
    assert op.status(t) == "New"
