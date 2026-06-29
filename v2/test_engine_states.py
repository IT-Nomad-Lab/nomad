#!/usr/bin/env python3
"""NOMAD v2 engine — state-machine unit tests (stdlib only, no pytest/network).

Complements test_engine.py (the P1-3 integration test on live NocoDB/LiteLLM). This one is
deterministic and OFFLINE: it injects a FakeDB and drives resume_run/retry_run directly against a
seeded proposal row — no Manager LLM, no specialists. Covers the gate/execute logic the cockpit
relies on: approve→executed, the distinct failed state (tool returns ok:False, tool raises,
unknown action) vs operator reject→declined, idempotency of terminal states, and retry
(failed→awaiting-approval, error cleared). Run:  python3 test_engine_states.py
"""
import json

from engine import Engine


class FakeDB:
    """In-memory stand-in for the NocoDB client — just the methods the engine uses."""
    def __init__(self):
        self.t = {"comms": {}, "goals": {}, "episodic": {}}
        self._id = 0

    def create(self, table, obj):
        self._id += 1
        row = {**obj, "Id": self._id}
        self.t[table][self._id] = row
        return row

    def find(self, table, col, val):
        for r in self.t[table].values():
            if r.get(col) == val:
                return r
        return None

    def update(self, table, _id, patch):
        self.t[table][_id].update(patch)
        return self.t[table][_id]

    def list(self, table, limit=50):
        return list(self.t[table].values())[:limit]


def _seed(db, action="send_message", args=None, run_id="r1", status="awaiting-approval"):
    """Seed a goal + a paused comms row carrying a proposal, as start_run would leave it."""
    goal = db.create("goals", {"title": "t", "status": "routed"})
    payload = {"action": action, "args": args or {"to": "x@y.z", "body": "hi"},
               "goal": "t", "intent": "i", "target": "x@y.z"}
    db.create("comms", {"run_id": run_id, "lane": "comms", "status": status, "type": "proposal",
                        "goal_id": goal["Id"], "payload": json.dumps(payload)})
    return run_id


def _eng(ok=True, summary="sent", sink_id=None, raises=False):
    db = FakeDB()
    e = Engine(db=db)

    def tool(**kw):
        if raises:
            raise RuntimeError("boom")
        r = {"ok": ok, "summary": summary, "error": "" if ok else "tool down"}
        if sink_id is not None:
            r["outbox_id"] = sink_id
        return r
    e.tools = {"send_message": tool}
    return e, db


CASES = []
def case(fn): CASES.append(fn); return fn


@case
def test_approve_executed():
    e, db = _eng(ok=True, sink_id=42)
    rid = _seed(db)
    out = e.resume_run(rid, "approved")
    assert out["status"] == "executed", out
    row = db.find("comms", "run_id", rid)
    assert row["status"] == "executed"
    assert db.find("goals", "Id", row["goal_id"])["status"] == "done"
    assert any("outbox#42" in (r.get("links") or "") for r in db.list("episodic")), "expected success log"


@case
def test_tool_failure_is_failed_not_declined():
    e, db = _eng(ok=False)
    rid = _seed(db)
    out = e.resume_run(rid, "approved")
    assert out["status"] == "failed", out
    row = db.find("comms", "run_id", rid)
    assert row["status"] == "failed"
    assert json.loads(row["payload"]).get("error") == "tool down"
    assert any("FAILED" in (r.get("outcome") or "") for r in db.list("episodic"))


@case
def test_tool_raises_is_failed():
    e, db = _eng(raises=True)
    rid = _seed(db)
    out = e.resume_run(rid, "approved")
    assert out["status"] == "failed", out
    assert "RuntimeError" in (json.loads(db.find("comms", "run_id", rid)["payload"]).get("error") or "")


@case
def test_unknown_action_is_failed():
    e, db = _eng()
    rid = _seed(db, action="not_a_tool")
    out = e.resume_run(rid, "approved")
    assert out["status"] == "failed", out
    assert db.find("comms", "run_id", rid)["status"] == "failed"


@case
def test_reject_is_declined():
    e, db = _eng()
    rid = _seed(db)
    out = e.resume_run(rid, "rejected")
    assert out["status"] == "declined", out
    assert db.find("comms", "run_id", rid)["status"] == "declined"


@case
def test_idempotent_terminal():
    for terminal in ("executed", "failed", "declined", "executing"):
        e, db = _eng()
        rid = _seed(db, status=terminal)
        out = e.resume_run(rid, "approved")
        assert out.get("note") == "already resolved (idempotent)", (terminal, out)


@case
def test_retry_failed_back_to_gate():
    e, db = _eng()
    rid = _seed(db, status="failed")
    row = db.find("comms", "run_id", rid)
    p = json.loads(row["payload"]); p["error"] = "n8n down"; row["payload"] = json.dumps(p)
    out = e.retry_run(rid)
    assert out["status"] == "awaiting-approval", out
    row = db.find("comms", "run_id", rid)
    assert row["status"] == "awaiting-approval"
    assert "error" not in json.loads(row["payload"]), "retry must clear the error"


@case
def test_retry_only_failed():
    e, db = _eng()
    rid = _seed(db, status="executed")
    out = e.retry_run(rid)
    assert out.get("note") == "only failed runs retry", out


if __name__ == "__main__":
    import sys
    passed = 0
    for fn in CASES:
        fn()
        print(f"  PASS  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(CASES)} engine state-machine tests passed")
    sys.exit(0)
