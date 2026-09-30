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

from engine import Engine, proposal_hash


def _hash(db, rid="r1"):
    """The proposal hash an approver would have read back from GET /run?id=."""
    return proposal_hash(json.loads(db.find("comms", "run_id", rid)["payload"]))


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

    def list(self, table, limit=50, where=None, sort=None):
        rows = list(self.t[table].values())
        if where:                      # only (field,eq,value) is used by the engine
            f, _, v = where.strip("()").split(",", 2)
            rows = [r for r in rows if str(r.get(f)) == v]
        if sort == "-Id":
            rows.sort(key=lambda r: r["Id"], reverse=True)
        return rows[:limit]


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
    out = e.resume_run(rid, "approved", _hash(db))
    assert out["status"] == "executed", out
    row = db.find("comms", "run_id", rid)
    assert row["status"] == "executed"
    assert db.find("goals", "Id", row["goal_id"])["status"] == "done"
    assert any("outbox#42" in (r.get("links") or "") for r in db.list("episodic")), "expected success log"


@case
def test_a_build_reports_its_feature_branch():
    e, db = _eng()
    e.tools = {"dispatch_build": lambda **kw: {"ok": True, "summary": "built", "branch": "feat/x"}}
    rid = _seed(db, action="dispatch_build", args={"project": "p", "task": "t", "run_id": "r1"})
    out = e.resume_run(rid, "approved", _hash(db))
    assert out["status"] == "executed", out
    assert out["outcome"].endswith("[branch feat/x]"), out["outcome"]


@case
def test_tool_failure_is_failed_not_declined():
    e, db = _eng(ok=False)
    rid = _seed(db)
    out = e.resume_run(rid, "approved", _hash(db))
    assert out["status"] == "failed", out
    row = db.find("comms", "run_id", rid)
    assert row["status"] == "failed"
    assert json.loads(row["payload"]).get("error") == "tool down"
    assert any("FAILED" in (r.get("outcome") or "") for r in db.list("episodic"))


@case
def test_tool_raises_is_failed():
    e, db = _eng(raises=True)
    rid = _seed(db)
    out = e.resume_run(rid, "approved", _hash(db))
    assert out["status"] == "failed", out
    assert "RuntimeError" in (json.loads(db.find("comms", "run_id", rid)["payload"]).get("error") or "")


@case
def test_unknown_action_is_failed():
    e, db = _eng()
    rid = _seed(db, action="not_a_tool")
    out = e.resume_run(rid, "approved", _hash(db))
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
def test_approve_without_expect_hash_is_refused():
    """The control F1 asked for: an approval that does not name its proposal does not execute."""
    fired = []
    e, db = _eng(ok=True, sink_id=7)
    inner = e.tools["send_message"]
    e.tools["send_message"] = lambda **kw: (fired.append(kw), inner(**kw))[1]
    rid = _seed(db)
    out = e.resume_run(rid, "approved")
    assert "expect_hash is required" in (out.get("error") or ""), out
    assert not fired, "the action fired despite a refused approval"
    assert db.find("comms", "run_id", rid)["status"] == "awaiting-approval", "run left the gate"


@case
def test_stale_hash_is_refused():
    e, db = _eng()
    rid = _seed(db)
    stale = _hash(db)
    row = db.find("comms", "run_id", rid)
    p = json.loads(row["payload"]); p["args"]["body"] = "edited after it was shown"
    db.update("comms", row["Id"], {"payload": json.dumps(p)})
    out = e.resume_run(rid, "approved", stale)
    assert "proposal changed" in (out.get("error") or ""), out
    assert db.find("comms", "run_id", rid)["status"] == "awaiting-approval"


@case
def test_db_origin_approves_without_a_hash():
    """NocoDB flips are the operator acting on the authoritative row; hashing it against itself
    would prove nothing. This path stays open, and only in-process callers can reach it."""
    e, db = _eng(ok=True, sink_id=9)
    rid = _seed(db)
    out = e.resume_run(rid, "approved", db_origin=True)
    assert out["status"] == "executed", out


@case
def test_db_origin_is_keyword_only():
    """A request body can set expect_hash. It must never be able to set db_origin, or the
    requirement is one JSON field away from being optional again."""
    e, db = _eng()
    rid = _seed(db)
    try:
        e.resume_run(rid, "approved", None, True)
    except TypeError:
        return
    raise AssertionError("db_origin was accepted positionally; a client could reach it")


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


@case
def test_hash_mismatch_refuses_approve():
    from engine import proposal_hash
    e, db = _eng(ok=True)
    rid = _seed(db)
    shown = proposal_hash(json.loads(db.find("comms", "run_id", rid)["payload"]))
    row = db.find("comms", "run_id", rid)
    p = json.loads(row["payload"]); p["args"]["to"] = "attacker@evil.test"
    row["payload"] = json.dumps(p)
    out = e.resume_run(rid, "approved", shown)
    assert "proposal changed" in (out.get("error") or ""), out
    assert db.find("comms", "run_id", rid)["status"] == "awaiting-approval"
    out = e.resume_run(rid, "approved", proposal_hash(p))
    assert out["status"] == "executed", out


@case
def test_concurrent_resume_executes_once():
    import threading
    e, db = _eng(ok=True)
    calls, gate = [], threading.Event()

    def slow(**kw):
        calls.append(kw); gate.wait(2)
        return {"ok": True, "summary": "sent"}
    e.tools = {"send_message": slow}
    rid = _seed(db)
    outs = []
    ph = _hash(db)
    ts = [threading.Thread(target=lambda: outs.append(e.resume_run(rid, "approved", ph))) for _ in range(5)]
    for t in ts: t.start()
    import time; time.sleep(0.3); gate.set()
    for t in ts: t.join()
    assert len(calls) == 1, f"tool ran {len(calls)} times"
    assert sum(o.get("status") == "executed" for o in outs) == 1, outs


def _seed_new(db, run_id, age_min):
    """A comms row abandoned in `new` `age_min` minutes ago, as a killed start_run leaves it."""
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=age_min)
    return db.create("comms", {"run_id": run_id, "lane": "comms", "status": "new",
                               "type": "goal", "payload": json.dumps({"goal": "t"}),
                               "created_at": ts.isoformat()})


@case
def test_sweep_fails_only_the_abandoned():
    """Old `new` rows get failed with a reason; young ones and non-`new` rows are untouched."""
    e, db = _eng()
    old = _seed_new(db, "stale", 200)
    young = _seed_new(db, "fresh", 5)
    _seed(db, run_id="waiting")                      # awaiting-approval, not ours to sweep
    swept = e.sweep_stalled(max_age_min=45)
    assert [s["comms_id"] for s in swept] == [old["Id"]], swept
    assert swept[0]["age_min"] >= 200
    stale = db.find("comms", "run_id", "stale")
    assert stale["status"] == "failed", stale
    assert "stalled in 'new'" in json.loads(stale["payload"])["error"]
    assert db.find("comms", "run_id", "fresh")["status"] == "new"
    assert db.find("comms", "run_id", "waiting")["status"] == "awaiting-approval"
    assert any(r["what"] == "swept a stalled run" for r in db.t["episodic"].values())


@case
def test_sweep_is_idempotent():
    """A swept run is terminal, so the next pass finds nothing — no repeated alerts."""
    e, db = _eng()
    _seed_new(db, "stale", 200)
    assert len(e.sweep_stalled(max_age_min=45)) == 1
    assert e.sweep_stalled(max_age_min=45) == []


@case
def test_sweep_survives_a_bad_timestamp():
    """A row with an unparseable created_at is skipped, not allowed to kill the sweep."""
    e, db = _eng()
    db.create("comms", {"run_id": "junk", "status": "new", "created_at": "not a date"})
    db.create("comms", {"run_id": "none", "status": "new"})
    old = _seed_new(db, "stale", 200)
    assert [s["comms_id"] for s in e.sweep_stalled(max_age_min=45)] == [old["Id"]]
    assert db.find("comms", "run_id", "junk")["status"] == "new"


if __name__ == "__main__":
    import sys
    passed = 0
    for fn in CASES:
        fn()
        print(f"  PASS  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(CASES)} engine state-machine tests passed")
    sys.exit(0)
