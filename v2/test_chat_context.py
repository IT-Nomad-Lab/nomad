"""Tests for the cockpit chat's two context sources: a named run (G3) and the stored transcript (G4).

Both exist because the chat could see a *summary* of the pipeline but not the record. Asked why a
specific run failed, it had the status and nothing else, so the only answer available to it was a
guess that reads exactly like a fact. And every turn was already in Qdrant while the cockpit threw
the conversation away on reload.

The interesting behaviour here is the refusals: a hex-looking word that is not a run must inject
nothing, and an ambiguous prefix must name its candidates rather than pick one. Injecting the
wrong run's error is worse than injecting none.

Run: python3 -m pytest v2/test_chat_context.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402


REAL = "25693e0adecf"
OTHER = "25693eff0000"          # shares a 6-char prefix with REAL
LONELY = "f7afa3734f63"


@pytest.fixture
def runs(monkeypatch):
    """A fake comms table: `db.find` answers for exact ids, `recent_runs` lists them."""
    rows = {
        REAL: {"run_id": REAL, "lane": "comms", "status": "failed",
               "payload": '{"goal": "mail the brief", "intent": "send", "target": "josias",'
                          ' "action": "send_message", "error": "SMTP 535 auth failed"}'},
        OTHER: {"run_id": OTHER, "lane": "ads", "status": "executed", "payload": "{}"},
        LONELY: {"run_id": LONELY, "lane": "dev", "status": "executed", "payload": "{}"},
    }
    monkeypatch.setattr(server.db, "find",
                        lambda table, field, val: rows.get(val), raising=False)
    monkeypatch.setattr(server, "recent_runs",
                        lambda limit=25: [{"run_id": k, "status": v["status"]}
                                          for k, v in rows.items()])
    monkeypatch.setattr(server.db, "list", lambda table, n=200, **kw: [], raising=False)
    return rows


# ── G3: resolving what the operator typed ────────────────────────────────────────────────────

def test_a_full_run_id_resolves(runs):
    assert server._resolve_run_ids(f"why did run {REAL} fail?") == ([REAL], [])


def test_a_unique_prefix_resolves(runs):
    """Nobody retypes twelve hex characters, so a prefix has to work."""
    resolved, ambiguous = server._resolve_run_ids("what happened with f7afa3")
    assert resolved == [LONELY] and ambiguous == []


def test_an_ambiguous_prefix_names_its_candidates_instead_of_guessing(runs):
    resolved, ambiguous = server._resolve_run_ids("tell me about 25693e")
    assert resolved == []
    tok, hits = ambiguous[0]
    assert tok == "25693e" and set(hits) == {REAL, OTHER}


def test_a_hex_looking_word_that_is_not_a_run_injects_nothing(runs):
    """'deface' and 'decade' are hex. The regex is not the filter; resolution against the real
    run list is, which is why a false positive costs nothing."""
    assert server._resolve_run_ids("the deface decade added facade") == ([], [])


def test_an_ordinary_question_touches_nothing(runs):
    assert server._resolve_run_ids("what is pending right now?") == ([], [])


def test_at_most_two_runs_are_injected(runs):
    resolved, _ = server._resolve_run_ids(f"compare {REAL} {OTHER} {LONELY}")
    assert len(resolved) == 2


# ── G3: what the model actually sees ─────────────────────────────────────────────────────────

def test_the_block_carries_the_real_error(runs):
    """The whole point: 'why did run X fail' gets the actual error, not a guess."""
    block = server._run_context(*server._resolve_run_ids(f"why did {REAL} fail"))
    assert "SMTP 535 auth failed" in block
    assert "failed" in block and "comms" in block
    assert "mail the brief" in block


def test_the_block_tells_the_model_to_quote_the_record(runs):
    block = server._run_context([REAL], [])
    assert "speculat" in block.lower() or "quote the actual error" in block


def test_an_ambiguous_prefix_reaches_the_model_as_a_question(runs):
    block = server._run_context(*server._resolve_run_ids("about 25693e"))
    assert "matches 2 runs" in block and "ask which one" in block


def test_no_runs_means_no_block(runs):
    assert server._run_context([], []) == ""


def test_an_unknown_id_is_skipped_not_rendered_empty(runs, monkeypatch):
    monkeypatch.setattr(server, "run_detail", lambda rid: {"run_id": rid, "status": None})
    assert server._run_context([REAL], []) == ""


def test_a_lookup_failure_does_not_take_the_chat_turn_down(runs, monkeypatch):
    """Chat answering worse is survivable; chat failing the whole turn because a run lookup
    broke is not. The operator asking about a run is often asking *because* something is wrong."""
    import memory_chat
    monkeypatch.setattr(server, "run_detail",
                        lambda rid: (_ for _ in ()).throw(RuntimeError("nocodb down")))
    monkeypatch.setattr(server, "_pipeline_context", lambda limit=12: "idle")
    monkeypatch.setattr(memory_chat, "classify_memory_intent", lambda t: None)
    monkeypatch.setattr(memory_chat, "recall", lambda *a, **k: [])
    monkeypatch.setattr(memory_chat, "remember", lambda *a, **k: None)
    seen = {}
    monkeypatch.setitem(sys.modules, "runtime", type(sys)("runtime"))
    # no run_tools: the tool turn can't start, so this is the no-tools fallback (#970 R5)
    sys.modules["runtime"].TOOL_TIMEOUT_S = 60
    sys.modules["runtime"].run = lambda system, user, alias=None, max_tokens=0, timeout_s=None: (
        seen.setdefault("system", system), "an answer")[1]

    out = server.chat_reply(f"why did {REAL} fail", session_id="t")
    assert out["reply"].startswith("an answer")
    assert "RUN DETAIL" not in seen["system"]     # degraded, not dead


# ── G4: the transcript comes back ────────────────────────────────────────────────────────────

def test_history_is_oldest_first(monkeypatch):
    """Scroll does not order, which is what `ts` was stored for."""
    import memory_chat
    monkeypatch.setattr(memory_chat, "ensure_collection", lambda: True)

    class _R:
        @staticmethod
        def json():
            return {"result": {"points": [
                {"payload": {"role": "assistant", "content": "second", "ts": 2}},
                {"payload": {"role": "user", "content": "first", "ts": 1}},
            ]}}
    monkeypatch.setattr(memory_chat.requests, "post", lambda *a, **k: _R())
    turns = memory_chat.history("cockpit")
    assert [t["content"] for t in turns] == ["first", "second"]
    assert [t["role"] for t in turns] == ["user", "assistant"]


def test_history_fails_open(monkeypatch):
    """Qdrant being down must mean an empty transcript, never a broken cockpit."""
    import memory_chat
    monkeypatch.setattr(memory_chat, "ensure_collection", lambda: True)

    def boom(*a, **k):
        raise OSError("qdrant unreachable")
    monkeypatch.setattr(memory_chat.requests, "post", boom)
    assert memory_chat.history("cockpit") == []
    monkeypatch.setattr(memory_chat, "ensure_collection", lambda: False)
    assert memory_chat.history("cockpit") == []


# ── S4: the cockpit has to show whether the thing actually left ──────────────────────────────

def test_delivery_reports_an_unrecorded_status_rather_than_guessing(monkeypatch):
    """Rows written before the `status` column was added were stamped `delivered_at` whether or not
    the send worked. Calling them delivered now would invent the fact the column exists to stop us
    inventing, so they report as `unrecorded`."""
    import server
    monkeypatch.setattr(server.db, "list",
                        lambda t, n=20, where=None, sort=None: [
                            {"Id": 7, "to": "a@b.c", "delivered_at": "2026-07-01T00:00:00Z"}])
    out = server._delivery("r1")
    assert out == [{"outbox_id": 7, "to": "a@b.c", "status": "unrecorded", "provider_id": "",
                    "delivered_at": "2026-07-01T00:00:00Z", "error": ""}]


def test_delivery_carries_the_failure_reason(monkeypatch):
    import server
    monkeypatch.setattr(server.db, "list",
                        lambda t, n=20, where=None, sort=None: [
                            {"Id": 8, "to": "a@b.c", "status": "failed",
                             "delivered_at": None, "error": "HTTPError: 403"}])
    out = server._delivery("r1")
    assert out[0]["status"] == "failed" and "403" in out[0]["error"]
    assert out[0]["delivered_at"] is None


def test_delivery_filters_at_nocodb_rather_than_scanning(monkeypatch):
    """NocoDB returns oldest-first, so a blind `limit` scan drops exactly the newest rows. The
    filter has to go in the query."""
    import server
    seen = {}

    def fake_list(t, n=20, where=None, sort=None):
        seen["table"], seen["where"] = t, where
        return []
    monkeypatch.setattr(server.db, "list", fake_list)
    server._delivery("run-xyz")
    assert seen["table"] == "outbox"
    assert seen["where"] == "(run_id,eq,run-xyz)"


def test_delivery_survives_a_missing_table(monkeypatch):
    """Run detail is how an operator diagnoses a bad run. It must not 500 because the audit table
    is unreachable."""
    import server

    def boom(*a, **k):
        raise RuntimeError("no such table")
    monkeypatch.setattr(server.db, "list", boom)
    assert server._delivery("r1") == []


# ── S6: `sent` is a claim, the provider id is the receipt behind it ───────────────────────────

def test_delivery_carries_the_provider_id_so_the_claim_can_be_checked(monkeypatch):
    """`sent` is this system's word. Gmail's message id is the thing an operator can take to the
    mailbox, so passing it through is what makes the status auditable instead of trusted."""
    import server
    monkeypatch.setattr(server.db, "list",
                        lambda t, n=20, where=None, sort=None: [
                            {"Id": 9, "to": "a@b.c", "status": "sent",
                             "provider_id": "18f2c0abcd",
                             "delivered_at": "2026-09-23T10:00:00Z", "error": ""}])
    out = server._delivery("r1")
    assert out[0]["status"] == "sent" and out[0]["provider_id"] == "18f2c0abcd"


def test_delivery_reports_no_provider_id_as_empty_not_missing(monkeypatch):
    """An `accepted` row has no id by definition. The key is still present so the cockpit does not
    have to tell a row written before the column from one that simply has nothing to show."""
    import server
    monkeypatch.setattr(server.db, "list",
                        lambda t, n=20, where=None, sort=None: [
                            {"Id": 10, "to": "a@b.c", "status": "accepted",
                             "delivered_at": "2026-09-23T10:00:00Z", "error": ""}])
    out = server._delivery("r1")
    assert out[0]["provider_id"] == ""


def test_an_unknown_send_reaches_the_cockpit_as_unknown(monkeypatch):
    """The status the operator most needs to see, because it is the one where approving a retry
    can send the same message twice."""
    import server
    monkeypatch.setattr(server.db, "list",
                        lambda t, n=20, where=None, sort=None: [
                            {"Id": 11, "to": "a@b.c", "status": "unknown", "provider_id": "",
                             "delivered_at": None,
                             "error": "TimeoutError — timed out waiting for Gmail."}])
    out = server._delivery("r1")
    assert out[0]["status"] == "unknown"
    assert out[0]["delivered_at"] is None
    assert "timed out" in out[0]["error"]


def test_the_cockpit_actually_renders_delivery(monkeypatch):
    """The API has carried these rows since S4 and the page never drew them, so the honest record
    stopped one layer short of the only person who reads it."""
    import server
    assert "r.delivery" in server.COCKPIT
    for st in ("sent", "accepted", "unknown", "failed", "unrecorded"):
        assert st in server.COCKPIT, st


def test_the_cockpit_escapes_what_it_draws(monkeypatch):
    """`to` and `error` come from the outside: an address and an upstream error string."""
    import server
    block = server.COCKPIT.split("r.delivery.forEach")[1][:600]
    for field in ("d.to", "d.provider_id", "d.error", "st.toUpperCase()"):
        assert f"esc({field}" in block, field
