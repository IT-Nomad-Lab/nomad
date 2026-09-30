"""#778 · the cockpit approves the proposal it showed, not the one that exists at click time.

The engine's hash check was always right: an approval names a proposal_hash, and a hash that no
longer matches the stored proposal is refused. The cockpit defeated it. Its gate() fetched /run at
click time and sent back *that* hash, so the check compared the proposal with itself. A proposal
that changed after the page was painted was approved without the operator ever seeing it, and the
comment above the code said the opposite. The Zulip relay did this right all along: it pins the
hash of the message it posted.

So the runs feed now carries the hash of each preview it serves, the page remembers the hash of
what it drew, and gate() sends that. The page tests are about the script, since the container has
no browser; a headless-browser check against the real page is in the #778 notes.

Run: python3 -m pytest v2/test_cockpit_gate.py -q   (engine deps live in the container)
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402
from engine import proposal_hash  # noqa: E402

PAGE = server.COCKPIT


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def list(self, table, limit=50, where=None, sort=None):
        assert table == "comms"
        return sorted(self.rows, key=lambda r: r["Id"], reverse=True)[:limit]


def comms(i, status, payload):
    return {"Id": i, "run_id": f"run-{i}", "lane": "dev", "from_agent": "manager",
            "type": "proposal", "status": status, "created_at": f"2026-09-26T10:0{i}:00",
            "payload": json.dumps(payload)}


BUILD = {"action": "dispatch_build",
         "args": {"project": "nomad", "task": "one line", "plan": "the long plan " * 200}}


def _fn(name):
    """The body of one top-level `async function name(...)` in the cockpit script."""
    m = re.search(r"async function " + name + r"\(.*?\n}\n", PAGE, re.S)
    assert m, f"{name}() is gone from the cockpit"
    return m.group(0)


# ── the feed ─────────────────────────────────────────────────────────────────────────────────

def test_the_feed_carries_the_hash_of_the_proposal_it_previews(monkeypatch):
    monkeypatch.setattr(server, "db", FakeDB([comms(1, "awaiting-approval", BUILD)]))
    [run] = server.recent_runs()
    assert run["proposal"]["proposal_hash"] == proposal_hash(BUILD)


def test_the_hash_covers_the_whole_proposal_not_the_cut_preview(monkeypatch):
    """The preview is cut at 280 characters. Two plans that differ after that point must still get
    different hashes, or the pin would let an unseen tail change through."""
    other = {**BUILD, "args": {**BUILD["args"], "plan": BUILD["args"]["plan"] + "and delete it"}}
    monkeypatch.setattr(server, "db", FakeDB([comms(1, "awaiting-approval", BUILD),
                                              comms(2, "awaiting-approval", other)]))
    runs = {r["run_id"]: r for r in server.recent_runs()}
    assert runs["run-1"]["proposal"]["preview"] == runs["run-2"]["proposal"]["preview"]
    assert runs["run-1"]["proposal"]["proposal_hash"] != runs["run-2"]["proposal"]["proposal_hash"]


def test_a_decided_run_carries_no_proposal(monkeypatch):
    monkeypatch.setattr(server, "db", FakeDB([comms(1, "executed", BUILD)]))
    [run] = server.recent_runs()
    assert "proposal" not in run


# ── the page ─────────────────────────────────────────────────────────────────────────────────

def test_approve_does_not_read_the_proposal_again():
    """The bug itself: a fetch of /run inside gate() is a fresh hash, and a fresh hash approves
    whatever is there now."""
    assert "fetch('/run" not in _fn("gate")


def test_approve_sends_the_hash_the_page_drew():
    body = _fn("gate")
    assert "shownHash[rid]" in body
    assert "body.expect_hash=h" in body


def test_approve_refuses_when_the_page_drew_no_hash():
    body = _fn("gate")
    assert re.search(r"if\(!h\)\{.*?return;", body, re.S), "no drawn hash must stop the approval"


def test_the_engine_refusal_is_shown_not_swallowed():
    """/resume answers a stale hash with a 200 and an `error`. The old page never read the reply,
    so a refused approval looked exactly like an accepted one."""
    body = _fn("gate")
    assert "res.error" in body
    assert "changed since you opened it" in body


def test_the_row_pins_the_hash_it_rendered():
    tick = _fn("tick")
    assert "shownHash[rid]=(p&&p.proposal_hash)||null" in tick


def test_a_detail_that_disagrees_with_the_row_turns_approve_off():
    detail = _fn("loadDetail")
    assert re.search(r"r\.proposal_hash!==shownHash\[rid\]\)\{\s*shownHash\[rid\]=null", detail)
