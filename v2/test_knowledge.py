"""V2c · the research brief, from the chat turn that asks for it to the copy that got stored.

This is the last console-only row with a functional gap. The console answered "research X" by
calling a model and handing the brief straight back, then filing it to a Notion database. Both
halves were wrong under v2: the model call had no gate in front of it, and the Notion write refuses
now because Notion is the rollback source.

The v2 sink has existed since 2A — the research lane files to the NocoDB `knowledge` table through
`save_brief`, after approval. What did not exist was any way to read it back, so a brief could be
filed correctly and still be invisible. That is the same shape as S6: the tool returned, and the
tool returning is not the artifact existing.

So these tests care about two things. That the read path reports what is in the table rather than
what the run's status implies. And that the write path stays exactly where it is, behind the gate,
because a second way in would skip the approval the first one has.

Run: python3 -m pytest v2/test_knowledge.py -q   (engine deps live in the container)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402


class FakeDB:
    """A NocoDB stand-in that records how it was queried, because the filtering is the point.

    `list` returning oldest-first with a blind limit is a bug this project has already shipped
    twice, so the calls are kept and asserted on rather than just their results."""

    def __init__(self, tables=None):
        self.tables = tables or {}
        self.calls = []

    def list(self, table, limit=50, where=None, sort=None):
        self.calls.append({"table": table, "limit": limit, "where": where, "sort": sort})
        if table not in self.tables:
            raise RuntimeError(f"no such table {table}")
        rows = self.tables[table]
        if where and ",eq," in where:
            field, val = where.strip("()").split(",eq,")
            rows = [r for r in rows if str(r.get(field, "")) == val]
        if where and ",like," in where:
            field, val = where.strip("()").split(",like,")
            rows = [r for r in rows if val.strip("%").lower() in str(r.get(field, "")).lower()]
        if sort == "-Id":
            rows = sorted(rows, key=lambda r: r.get("Id", 0), reverse=True)
        return rows[:limit]


def brief(i, run="r-1", topic="local STT engines", body="the brief body"):
    return {"Id": i, "topic": topic, "brief": body, "run_id": run,
            "created_at": f"2026-09-2{i % 10}T10:00:00"}


@pytest.fixture
def db(monkeypatch):
    d = FakeDB({"knowledge": [], "content": []})
    monkeypatch.setattr(server, "db", d)
    return d


# ── reading a run's filed artifacts ─────────────────────────────────────────────────────────────

def test_a_run_reports_the_brief_it_filed(db):
    db.tables["knowledge"] = [brief(4, run="r-7", topic="Piper voices", body="findings")]
    arts = server._artifacts("r-7")
    assert len(arts) == 1
    assert arts[0]["sink"] == "knowledge"
    assert arts[0]["id"] == 4
    assert arts[0]["title"] == "Piper voices"
    assert arts[0]["body"] == "findings"
    assert arts[0]["chars"] == len("findings")


def test_the_body_is_the_stored_copy_not_the_draft(db):
    """The proposal's `brief` arg is what was drafted; this is what the table holds. They should
    match, and "should match" is the assumption the outbox already caught us making."""
    db.tables["knowledge"] = [brief(1, run="r-1", body="STORED")]
    assert server._artifacts("r-1")[0]["body"] == "STORED"


def test_a_run_that_filed_nothing_reports_nothing(db):
    db.tables["knowledge"] = [brief(1, run="r-other")]
    assert server._artifacts("r-1") == []


def test_artifacts_are_filtered_at_nocodb_and_sorted_newest_first(db):
    """Not scanned. NocoDB returns oldest-first, so a blind limit drops the newest rows, which are
    exactly the ones an operator opening a run is looking for."""
    server._artifacts("r-9")
    for c in db.calls:
        assert c["where"] == "(run_id,eq,r-9)"
        assert c["sort"] == "-Id"


def test_content_runs_report_their_artifact_too(db):
    db.tables["content"] = [{"Id": 3, "topic": "launch post", "content": "copy",
                             "run_id": "r-2", "created_at": "2026-09-23T10:00:00"}]
    arts = server._artifacts("r-2")
    assert [(a["sink"], a["title"]) for a in arts] == [("content", "launch post")]


def test_a_missing_sink_table_does_not_break_the_drill_down(monkeypatch):
    """Only `knowledge` exists here. The `content` lookup raises, and the operator must still get
    the brief rather than an empty detail pane that looks like nothing was filed."""
    monkeypatch.setattr(server, "db", FakeDB({"knowledge": [brief(1, run="r-1")]}))
    assert len(server._artifacts("r-1")) == 1


def test_a_long_body_is_cut_and_says_so(db):
    """Silently truncating reads as a complete document that stops early, which is worse than
    refusing to show it."""
    db.tables["knowledge"] = [brief(1, run="r-1", body="x" * (server.ARTIFACT_MAX + 500))]
    a = server._artifacts("r-1")[0]
    assert a["truncated"] is True
    assert len(a["body"]) == server.ARTIFACT_MAX
    assert a["chars"] == server.ARTIFACT_MAX + 500      # the real length, not the shown one


def test_a_short_body_is_not_marked_truncated(db):
    db.tables["knowledge"] = [brief(1, run="r-1", body="short")]
    assert server._artifacts("r-1")[0]["truncated"] is False


def test_run_detail_carries_the_artifacts(monkeypatch, db):
    db.tables["knowledge"] = [brief(2, run="r-5")]
    monkeypatch.setattr(db, "find", lambda t, f, v: {"run_id": "r-5", "lane": "research",
                                                     "status": "executed", "payload": "{}"},
                        raising=False)
    monkeypatch.setattr(server, "_delivery", lambda rid: [])
    d = server.run_detail("r-5")
    assert [a["id"] for a in d["artifacts"]] == [2]


# ── the knowledge base ──────────────────────────────────────────────────────────────────────────

def test_briefs_list_newest_first(db):
    db.tables["knowledge"] = [brief(1, topic="one"), brief(2, topic="two"), brief(3, topic="three")]
    out = server._knowledge_list(50, "")
    assert [b["id"] for b in out["briefs"]] == [3, 2, 1]
    assert db.calls[-1]["sort"] == "-Id"


def test_the_list_gives_a_length_not_the_whole_brief(db):
    """The index is for finding one. Shipping every body in it would make the list grow with the
    corpus and be slowest exactly when there is most to look through."""
    db.tables["knowledge"] = [brief(1, body="a much longer body than the title")]
    b = server._knowledge_list(50, "")["briefs"][0]
    assert "brief" not in b
    assert b["chars"] == len("a much longer body than the title")


def test_a_query_narrows_by_topic(db):
    db.tables["knowledge"] = [brief(1, topic="Piper voices"), brief(2, topic="WSL2 networking")]
    out = server._knowledge_list(50, "piper")
    assert [b["topic"] for b in out["briefs"]] == ["Piper voices"]
    assert db.calls[-1]["where"] == "(topic,like,%piper%)"


def test_no_query_sends_no_filter(db):
    server._knowledge_list(50, "")
    assert db.calls[-1]["where"] is None


@pytest.mark.parametrize("asked,used", [(0, 1), (-5, 1), (10, 10), (5000, 200)])
def test_the_limit_is_clamped_to_something_sane(db, asked, used):
    server._knowledge_list(asked, "")
    assert db.calls[-1]["limit"] == used


def test_one_brief_comes_back_whole(db):
    db.tables["knowledge"] = [brief(7, body="the entire brief, uncut")]
    assert server._knowledge_one(7)["brief"] == "the entire brief, uncut"


def test_a_brief_that_is_not_there_says_so(db):
    assert "no brief" in server._knowledge_one(99)["error"]


def test_a_dead_database_reports_an_error_rather_than_an_empty_shelf(monkeypatch):
    """An empty list would read as "no briefs have ever been filed", which is a different and much
    more alarming fact than "the database is down"."""
    monkeypatch.setattr(server, "db", FakeDB({}))       # every table raises
    out = server._knowledge_list(50, "")
    assert out["briefs"] == []
    assert "unavailable" in out["error"]
    assert "unavailable" in server._knowledge_one(1)["error"]


# ── filing stays behind the gate ────────────────────────────────────────────────────────────────

def test_the_knowledge_route_is_read_only():
    """A write route here would be a second way into the knowledge base, and the first one is
    behind the human gate. Two ways in means the gate is optional."""
    routes = [r for r in server.app.routes if getattr(r, "path", "") == "/api/knowledge"]
    assert routes, "the route is missing"
    methods = set().union(*(r.methods for r in routes))
    assert methods <= {"GET", "HEAD"}, f"the knowledge base accepts {methods}"


def test_the_research_intent_reaches_no_handler_from_chat():
    """#973: "research X" is often a question, and the chat answers questions. No surface routes
    the research intent to a handler; it gets the pointer reply instead, and #984 deleted the
    handler."""
    import intents
    i, payload = intents.classify("research the best local STT engine")
    assert (i, payload) == ("research", {"topic": "the best local STT engine"})
    assert i not in server.ACTION_INTENTS
    assert all(i not in allowed for allowed in server.SURFACE_INTENTS.values())


def test_nothing_in_the_knowledge_path_writes_to_notion():
    """The console's `_kb_save` wrote to a Notion database. Notion is the rollback source and a
    mirror still being written to is not one, which is why that write refuses now."""
    import inspect
    for fn in (server._knowledge_list, server._knowledge_one, server._artifacts):
        assert "notion" not in inspect.getsource(fn).lower().split('"""')[-1]
