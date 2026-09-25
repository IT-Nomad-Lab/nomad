"""Tests for the read-only tools a specialist may call while drafting.

Most of these are about what must NOT happen. Process runs upstream of the human gate, and the gate
only means something because drafting cannot cause a side effect. So the load-bearing assertions
here are the refusals: a write tool named by the model is refused rather than executed, a lane
cannot reach a tool it was not given, and the write implementations are not even importable from
the module that does the dispatching.

Run: docker compose exec nomad-v2-engine sh -c 'python -m pytest test_drafting.py -q'
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import drafting  # noqa: E402


# ── the boundary ─────────────────────────────────────────────────────────────────────────────

def test_a_write_tool_is_refused_not_executed():
    """The whole reason this module exists. `send_message` is a real action NOMAD can take; naming
    it here must produce a refusal, not an email."""
    out = drafting.call("send_message", {"to": "someone@example.com", "body": "hi"})
    assert out.startswith("refused:")
    assert "read-only" in out
    assert "gate" in out            # and it tells the model where that request actually belongs


@pytest.mark.parametrize("name", ["save_brief", "save_content", "dispatch_build",
                                  "generate_image", "resume_run", "__import__", "eval"])
def test_nothing_outside_the_allowlist_dispatches(name):
    assert drafting.call(name, {}).startswith("refused:")


def test_a_lane_cannot_reach_a_tool_it_was_not_given():
    """Least privilege at the second level: the tool is read-only and still not this lane's."""
    out = drafting.call("web_search", {"query": "x"}, allowed=["recall"])
    assert out.startswith("refused:")
    assert "not available to this specialist" in out


def test_the_dispatcher_cannot_see_a_write_implementation():
    """Structural, not behavioural. Every *_impl is absent from this module's namespace, so no bug
    and no careless edit can reach one from the drafting path. Unused is weaker than unreachable."""
    leaked = [n for n in vars(drafting) if n.endswith("_impl")]
    assert leaked == []
    src = open(os.path.join(os.path.dirname(drafting.__file__), "drafting.py"), encoding="utf-8").read()
    for mod in ("mcp_comms", "mcp_content", "mcp_dev", "mcp_research", "mcp_image"):
        assert f"import {mod}" not in src, f"drafting.py must not import {mod}"


def test_every_allowlisted_tool_is_callable_and_described():
    for name, (fn, doc) in drafting.READ_TOOLS.items():
        assert callable(fn) and doc


def test_the_advertised_call_is_valid_json_the_model_can_copy():
    """The first live run failed on exactly this: the tool block showed Python repr with an
    ellipsis, so the model sent a bare TOOL: line, got nothing twice, and wrote a vague email about
    "the same details as before". An unusable tool is worse than no tool."""
    block = drafting.tool_block(list(drafting.READ_TOOLS))
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("TOOL: "):
            continue
        name, _, payload = line[len("TOOL: "):].partition(" ")
        assert name in drafting.READ_TOOLS
        args = json.loads(payload)                    # must parse, or the model cannot copy it
        assert args, f"{name} advertises no arguments"
        assert drafting._CALL_RE.match(line), f"the block's own example does not match the parser"


def test_a_call_missing_its_arguments_says_exactly_what_to_send():
    for tool, arg in (("prior_messages", "to"), ("recall", "query"),
                      ("web_search", "query"), ("read_url", "url")):
        out = drafting.call(tool, {})
        assert f"no {arg} given" in out
        assert f"TOOL: {tool} {{" in out
        json.loads(out.split(f"TOOL: {tool} ", 1)[1])   # the remedy it offers is itself valid


# ── refusing to read the machine's own insides ───────────────────────────────────────────────

@pytest.mark.parametrize("url", ["http://127.0.0.1:8099/runs", "http://localhost:5678/",
                                 "http://169.254.169.254/latest/meta-data/",
                                 "http://[::1]:8095/", "file:///etc/passwd", "not a url"])
def test_an_internal_or_non_http_url_is_refused(url):
    """Every NOMAD service is bound to 127.0.0.1, which makes loopback the most sensitive network
    here. The model picks this url, so it must not be able to point the scraper at the engine."""
    assert drafting._safe_url(url) != ""


def test_read_url_refuses_before_it_fetches(monkeypatch):
    """The refusal has to happen in front of the scraper call, not after it."""
    import scraper
    monkeypatch.setattr(scraper, "gather",
                        lambda *a, **k: pytest.fail("scraper was called for a blocked url"))
    assert drafting._read_url(url="http://127.0.0.1:8099/runs").startswith("refused:")


# ── the loop ─────────────────────────────────────────────────────────────────────────────────

def _runner(replies):
    """A fake model: returns each reply in turn and records the prompts it was given."""
    seen = []

    def run(system, user, alias="balanced", max_tokens=0):
        seen.append({"system": system, "user": user})
        return replies[min(len(seen) - 1, len(replies) - 1)]
    return run, seen


def test_a_specialist_looks_something_up_and_writes_from_it(monkeypatch):
    """The acceptance criterion: a read-only tool is called mid-draft and the result reaches the
    draft. The trace is what lets the gate show the operator where the facts came from."""
    monkeypatch.setitem(drafting.READ_TOOLS, "recall",
                        (lambda query="", **k: "- [comms] invoice chased → paid", "t"))
    run, seen = _runner(['TOOL: recall {"query": "invoice"}',
                         "We chased this invoice before and it was paid."])
    text, trace = drafting.draft("SKILL", "Intent: follow up\n", "balanced", 700,
                                 allowed=["recall"], runner=run)
    assert text == "We chased this invoice before and it was paid."
    assert trace == [{"tool": "recall", "args": {"query": "invoice"},
                      "result": "- [comms] invoice chased → paid"}]
    assert "invoice chased" in seen[1]["user"]       # the observation reached the second turn


def test_the_model_asking_for_a_write_tool_gets_a_refusal_and_carries_on(monkeypatch):
    """A model that tries to send the message itself must be told no and still produce a draft.
    Refusing has to be survivable, or someone widens the allowlist to make the error go away."""
    sent = []
    import mcp_comms
    monkeypatch.setattr(mcp_comms, "send_message_impl",
                        lambda **kw: sent.append(kw) or {"ok": True})
    run, seen = _runner(['TOOL: send_message {"to": "a@b.c", "body": "hi"}', "Here is the draft."])
    text, trace = drafting.draft("SKILL", "Intent: x\n", "balanced", 700,
                                 allowed=["recall"], runner=run)
    assert sent == []                                # nothing was sent
    assert text == "Here is the draft."              # and the draft still happened
    assert trace[0]["result"].startswith("refused:")
    assert "refused:" in seen[1]["user"]


def test_no_tools_means_no_loop_and_no_tool_instructions():
    """A lane with no read_tools must behave exactly as it did before: one call, plain prompt."""
    run, seen = _runner(["just the draft"])
    text, trace = drafting.draft("SKILL", "Intent: x", "balanced", 700, allowed=[], runner=run)
    assert (text, trace) == ("just the draft", [])
    assert len(seen) == 1
    assert "LOOKING THINGS UP" not in seen[0]["system"]


def test_lookups_are_capped():
    """Each lookup is a model round trip against subscription quota, so the budget must be exact:
    max_calls lookups and one closing turn, never an extra one."""
    run, seen = _runner(['TOOL: recall {"query": "x"}',
                         'TOOL: recall {"query": "y"}',
                         "finally the draft"])
    text, trace = drafting.draft("SKILL", "Intent: x", "balanced", 700,
                                 allowed=["recall"], runner=run, max_calls=2)
    assert text == "finally the draft"
    assert len(trace) == 2 and len(seen) == 3
    assert "No more lookups are available" in seen[2]["user"]


def test_a_model_that_never_stops_asking_fails_the_run_instead_of_drafting_a_tool_line():
    """A `TOOL:` line returned as content would be filed as a proposal and shown to the operator as
    if it were the drafted output. start_run catches this and fails the run with a reason."""
    run, _ = _runner(['TOOL: recall {"query": "x"}'])
    with pytest.raises(RuntimeError, match="never drafted"):
        drafting.draft("SKILL", "Intent: x", "balanced", 700,
                       allowed=["recall"], runner=run, max_calls=1)


def test_a_trailing_tool_line_is_stripped_rather_than_shipped():
    """The model answers but tacks on one more request. The answer is the draft; the line is not."""
    run, _ = _runner(['TOOL: recall {"query": "x"}',
                      'Here is the brief.\nTOOL: recall {"query": "more"}'])
    text, trace = drafting.draft("SKILL", "Intent: x", "balanced", 700,
                                 allowed=["recall"], runner=run, max_calls=1)
    assert text == "Here is the brief." and len(trace) == 1


def test_observations_are_framed_as_data_not_instructions():
    """A scraped page is written by a stranger. It is read, not obeyed."""
    run, seen = _runner(['TOOL: recall {"query": "x"}', "draft"])
    drafting.draft("SKILL", "Intent: x", "balanced", 700, allowed=["recall"], runner=run)
    assert "not an instruction" in seen[1]["user"]
    assert "never follow it" in seen[0]["system"]


def test_a_malformed_tool_line_does_not_crash_the_draft():
    run, _ = _runner(["TOOL: recall {not json at all}", "draft anyway"])
    text, trace = drafting.draft("SKILL", "Intent: x", "balanced", 700,
                                 allowed=["recall"], runner=run)
    assert text == "draft anyway" and trace[0]["args"] == {}


def test_a_tool_that_throws_becomes_an_observation_not_an_exception(monkeypatch):
    monkeypatch.setitem(drafting.READ_TOOLS, "recall",
                        (lambda **k: (_ for _ in ()).throw(RuntimeError("nocodb down")), "t"))
    out = drafting.call("recall", {"query": "x"})
    assert "recall failed" in out and "nocodb down" in out


def test_an_oversized_result_is_truncated(monkeypatch):
    monkeypatch.setitem(drafting.READ_TOOLS, "recall",
                        (lambda **k: "x" * 99999, "t"))
    out = drafting.call("recall", {"query": "x"})
    assert len(out) <= drafting.MAX_OBSERVATION + 20 and out.endswith("[truncated]")


# ── the lanes ────────────────────────────────────────────────────────────────────────────────

def test_every_lane_only_asks_for_read_only_tools():
    """A write name in LANES must be impossible to reach, whatever anyone types there."""
    import specialists
    for lane, cfg in specialists.LANES.items():
        for name in cfg.get("read_tools", []):
            assert name in drafting.READ_TOOLS, f"{lane} asks for non-read-only tool {name}"


def test_a_typo_in_a_lane_config_yields_no_tool_rather_than_a_bad_name():
    import specialists
    s = specialists.Specialist("comms", {**specialists.LANES["comms"],
                                         "read_tools": ["recall", "send_message", "nonsense"]})
    assert s.read_tools == ["recall"]


def test_propose_carries_the_lookups_to_the_gate(monkeypatch):
    """The operator approves the action; the lookups are the provenance shown alongside it."""
    import specialists
    monkeypatch.setattr(specialists.drafting, "draft",
                        lambda *a, **k: ("the body", [{"tool": "recall", "args": {},
                                                       "result": "found"}]))
    s = specialists.SPECIALISTS["comms"]
    monkeypatch.setattr(s, "style", None)            # skip the detector pass
    prop = s.propose("say hi", "someone@example.com", "abc123abc123")
    assert prop["lookups"][0]["tool"] == "recall"
    assert prop["action"] == "send_message"


def test_lookups_do_not_change_what_is_being_approved():
    """proposal_hash covers the action and args only. Provenance must not move the hash, or every
    approval would be invalidated by a detail the operator did not decide on."""
    import engine
    base = {"action": "send_message", "args": {"to": "a@b.c", "body": "hi"}}
    assert engine.proposal_hash(base) == engine.proposal_hash(
        {**base, "lookups": [{"tool": "recall", "args": {}, "result": "found"}]})


def test_prior_messages_reads_nocodbs_own_timestamp_column():
    """`created_at` is not a column on outbox; NocoDB's is `CreatedAt`. Reading the wrong one dated
    every message as empty and ordered them arbitrarily, so a model asked for "the last thing we
    sent" had an undated list and guessed. specialists._support_enrich had the same bug."""
    import drafting as d
    rows = [{"to": "a@b.c", "CreatedAt": "2026-07-16 11:36:11+00:00", "body": "older note", "Id": 1},
            {"to": "a@b.c", "CreatedAt": "2026-09-01 09:00:00+00:00", "body": "newer note", "Id": 2}]

    class _DB:
        def list(self, table, n=100, **kw):
            return list(reversed(rows))          # arrives unsorted, as NocoDB may return it
    import nocodb
    orig = nocodb.NocoDB
    nocodb.NocoDB = lambda *a, **k: _DB()
    try:
        out = d._prior_messages(to="a@b.c")
    finally:
        nocodb.NocoDB = orig
    assert "2026-07-16" in out and "2026-09-01" in out
    assert out.index("older note") < out.index("newer note")      # oldest first, newest last


def test_the_model_is_told_not_to_invent_specifics():
    """The first grounded live run cited a real prior message and also invented a Zulip key
    rotation the observations never mentioned. A lookup that grounds one detail and leaves the
    others fabricated is worse than none, because the draft now reads as researched."""
    block = drafting.tool_block(["recall"])
    assert "GROUND EVERY SPECIFIC" in block
    assert "cannot tell an invented specific from a real one" in block


def test_a_failed_send_is_not_shown_as_a_prior_message():
    """S4: an outbox row with status `failed` is an attempt, not a message. The recipient never saw
    it, so surfacing it here would have the specialist write "as I mentioned" about mail that never
    left, which is exactly the unfounded specific these lookups exist to prevent."""
    import drafting as d
    rows = [{"to": "a@b.c", "CreatedAt": "2026-09-01 09:00:00+00:00", "body": "arrived fine",
             "status": "accepted", "Id": 1},
            {"to": "a@b.c", "CreatedAt": "2026-09-02 09:00:00+00:00", "body": "never left",
             "status": "failed", "error": "HTTPError 403", "Id": 2}]

    class _DB:
        def list(self, table, n=100, **kw):
            return list(rows)
    import nocodb
    orig = nocodb.NocoDB
    nocodb.NocoDB = lambda *a, **k: _DB()
    try:
        out = d._prior_messages(to="a@b.c")
    finally:
        nocodb.NocoDB = orig
    assert "arrived fine" in out
    assert "never left" not in out


def test_rows_written_before_the_status_column_are_still_shown():
    """Rows that predate the migration have no status. They are not known to have failed, and
    dropping them would silently empty the thread history the lane depends on."""
    import drafting as d
    rows = [{"to": "a@b.c", "CreatedAt": "2026-07-16 11:36:11+00:00", "body": "legacy note", "Id": 1}]

    class _DB:
        def list(self, table, n=100, **kw):
            return list(rows)
    import nocodb
    orig = nocodb.NocoDB
    nocodb.NocoDB = lambda *a, **k: _DB()
    try:
        out = d._prior_messages(to="a@b.c")
    finally:
        nocodb.NocoDB = orig
    assert "legacy note" in out
