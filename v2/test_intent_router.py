"""V2c · the intent handlers, and the fact that they run before the chat model.

`test_intents.py` covers what the classifier matches. This covers what happens next: that a matched
command reaches a handler instead of the model, that the handlers say what they actually did, and
that the gate handler refuses to pick a run for the operator.

The classifier is the cheap half to get right. The handlers are the half that touches the pipeline,
and the one with an irreversible action at the end of it.

Run: python3 -m pytest v2/test_intent_router.py -q   (engine deps live in the container)
"""
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402


def _run(rid, status="awaiting-approval", action="send_message", target="a@b.c"):
    r = {"run_id": rid, "lane": "comms", "status": status, "created_at": "2026-09-23T10:00:00"}
    if status == "awaiting-approval":
        r["proposal"] = {"action": action, "target": target, "preview": "hello"}
    return r


@pytest.fixture
def quiet(monkeypatch):
    """No runs, no card, no heartbeat drift. Each test turns back on only what it is about."""
    monkeypatch.setattr(server, "recent_runs", lambda limit=25: [])
    monkeypatch.setattr(server, "_bump_runs", lambda: None)
    monkeypatch.setattr(server, "_proc_telemetry",
                        lambda: {"cpu_percent": 12.0, "mem_percent": 40.0, "gpu": None,
                                 "source": "proc"})
    monkeypatch.setitem(server._BEATS, "sweep", server.time.time())


# ── the router runs ahead of the model ──────────────────────────────────────────────────────────

@pytest.fixture
def chat(monkeypatch):
    """A chat_reply with memory and the model stubbed, so what is asserted is the routing."""
    import memory_chat
    import runtime
    calls = {"model": 0, "remembered": []}
    monkeypatch.setattr(memory_chat, "classify_memory_intent", lambda m: "")
    monkeypatch.setattr(memory_chat, "recall", lambda *a, **k: [])
    monkeypatch.setattr(memory_chat, "remember",
                        lambda s, role, c: calls["remembered"].append((role, c)))

    def _model(*a, **k):
        calls["model"] += 1
        return "the model answered"
    monkeypatch.setattr(runtime, "run", _model)
    return calls


def test_a_command_never_reaches_the_chat_model(chat, monkeypatch, quiet):
    """The whole reason the router exists. Handed "run diagnostics", a chat model describes a
    diagnostic; it does not read the gauges."""
    out = server.chat_reply("run diagnostics")
    assert chat["model"] == 0
    assert out["intent"] == "diagnostics"
    assert "diagnostic report" in out["reply"].lower()


def test_conversation_still_reaches_the_chat_model(chat, monkeypatch, quiet):
    monkeypatch.setattr(server, "_pipeline_context", lambda *a, **k: "idle")
    out = server.chat_reply("what does the comms lane do?")
    assert chat["model"] == 1
    assert (out["intent"], out["reply"]) == ("chat", "the model answered")


def test_an_action_turn_is_still_remembered(chat, quiet):
    """"What did you just do" is a fair question, and the answer has to live with the rest of the
    conversation rather than in a place only the pipeline can see."""
    out = server.chat_reply("run diagnostics")
    assert out["remembered"] is True
    assert [r for r, _ in chat["remembered"]] == ["user", "assistant"]


def test_off_the_record_still_holds_for_an_action(chat, monkeypatch, quiet):
    import memory_chat
    monkeypatch.setattr(memory_chat, "classify_memory_intent", lambda m: "off_record")
    out = server.chat_reply("run diagnostics")
    assert out["remembered"] is False
    assert chat["remembered"] == []


def test_forgetting_beats_the_router(chat, monkeypatch, quiet):
    """"Forget that" is a command too, and it is the one the operator is most entitled to have
    obeyed literally. It is checked first and returns before anything can classify it."""
    import memory_chat
    monkeypatch.setattr(memory_chat, "classify_memory_intent", lambda m: "wipe")
    monkeypatch.setattr(memory_chat, "forget", lambda *a, **k: 2)
    out = server.chat_reply("forget all of that, then run diagnostics")
    assert "wiped" in out["reply"]


def test_a_handler_that_throws_does_not_kill_the_turn(chat, monkeypatch, quiet):
    """A chat surface that returns a stack trace, or nothing, is worse than one that says the
    command failed. The operator can retype a command; they cannot recover a dropped turn."""
    monkeypatch.setitem(server.ACTION_INTENTS, "diagnostics",
                        lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    out = server.chat_reply("run diagnostics")
    assert "diagnostics failed" in out["reply"]
    assert "boom" in out["reply"]


def test_every_intent_the_classifier_emits_has_a_handler():
    """The contract `intents.INTENTS` exists for. An intent with no handler falls through to the
    model, which answers plausibly and does nothing, and nobody finds out."""
    import intents
    missing = [i for i in intents.INTENTS if i != "chat" and i not in server.ACTION_INTENTS]
    assert missing == []


def test_the_handler_table_invents_no_intents():
    import intents
    assert set(server.ACTION_INTENTS) <= set(intents.INTENTS)


# ── diagnostics ─────────────────────────────────────────────────────────────────────────────────

def test_diagnostics_says_it_did_not_check_the_services(quiet):
    """nomad-watch owns service health, on a five-minute timer, into Zulip `#alerts`. A report
    that quietly leaves that out reads as "everything is fine" to the person asking."""
    r = server._handle_diagnostics()
    assert "nomad-watch" in r


def test_a_card_that_will_not_answer_is_not_reported_as_idle(monkeypatch, quiet):
    """None from `_gpu` means no reading was possible. Rendering that as 0% would claim the GPU
    is free, which is exactly the thing worth knowing on this box and exactly the wrong answer."""
    r = server._handle_diagnostics()
    assert "no reading available" in r
    assert "0% util" not in r


def test_a_card_that_reports_only_its_name_says_so(monkeypatch, quiet):
    monkeypatch.setattr(server, "_proc_telemetry",
                        lambda: {"cpu_percent": 1.0, "mem_percent": 2.0,
                                 "gpu": {"name": "RTX 5090", "util": None, "mem_total": None}})
    r = server._handle_diagnostics()
    assert "would not report utilisation" in r


def test_a_real_card_reading_is_rendered(monkeypatch, quiet):
    monkeypatch.setattr(server, "_proc_telemetry",
                        lambda: {"cpu_percent": 1.0, "mem_percent": 2.0,
                                 "gpu": {"name": "RTX 5090", "util": 30.0, "mem_used": 6000.0,
                                         "mem_total": 24564.0, "temp": 62.0}})
    r = server._handle_diagnostics()
    assert "RTX 5090" in r and "30% util" in r and "62°C" in r


def test_pending_and_failed_runs_are_flagged_not_just_counted(monkeypatch, quiet):
    monkeypatch.setattr(server, "recent_runs",
                        lambda limit=25: [_run("a"), _run("b", "failed")])
    r = server._handle_diagnostics()
    assert "1 awaiting your decision" in r
    assert "1 failed" in r
    assert "Attention required" in r


def test_a_quiet_pipeline_reports_nominal(quiet):
    r = server._handle_diagnostics()
    assert "nominal" in r


def test_an_overdue_sweep_is_called_overdue(monkeypatch, quiet):
    """`active` is not the same as working. The sweep thread dying is silent: abandoned runs just
    stop being failed, with nobody waiting on them."""
    import engine as engine_mod
    monkeypatch.setitem(server._BEATS, "sweep",
                        server.time.time() - engine_mod.SWEEP_SECS * 10)
    assert "overdue" in server._handle_diagnostics()
    monkeypatch.setitem(server._BEATS, "sweep", server.time.time())
    assert "overdue" not in server._handle_diagnostics()


# ── capture ─────────────────────────────────────────────────────────────────────────────────────

def test_capture_says_nothing_runs_until_you_approve(monkeypatch, quiet):
    monkeypatch.setattr(server.eng, "start_run",
                        lambda goal, task_ref=None: {"run_id": "abc123", "lane": "comms",
                                                     "proposal": {"action": "send_message"}})
    r = server._handle_pipeline_capture("mail the brief")
    assert "abc123" in r and "comms" in r
    assert "Nothing runs until you do" in r


def test_an_empty_goal_asks_for_one_rather_than_filing_a_blank_run(quiet):
    assert "Give me a goal" in server._handle_pipeline_capture("")


def test_a_capture_that_throws_is_reported_not_swallowed(monkeypatch, quiet):
    monkeypatch.setattr(server.eng, "start_run",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nocodb down")))
    assert "nocodb down" in server._handle_pipeline_capture("x")


def test_research_captures_rather_than_researching(monkeypatch, quiet):
    """Research reaches the outside world, so it goes through the gate like everything else that
    does. The console learned this the same way."""
    seen = {}
    monkeypatch.setattr(server.eng, "start_run",
                        lambda goal, task_ref=None: seen.update(goal=goal) or
                        {"run_id": "r1", "lane": "research", "proposal": {"action": "research"}})
    server.ACTION_INTENTS["research"]({"topic": "local STT engines"})
    assert seen["goal"] == "Research and brief me on local STT engines"


# ── the gate ────────────────────────────────────────────────────────────────────────────────────

def test_nothing_pending_says_so(quiet):
    assert "Nothing is awaiting" in server._handle_gate_decision("approved", False)


@pytest.fixture(autouse=True)
def _no_pins():
    """Chat's record of what it showed lives for the process. Every test starts with none."""
    server._CHAT_SHOWN.clear()
    yield
    server._CHAT_SHOWN.clear()


def _approve_twice(decision="approved", do_all=False):
    """The first approve shows and pins; the second executes. Returns both replies."""
    return (server._handle_gate_decision(decision, do_all),
            server._handle_gate_decision(decision, do_all))


def test_the_first_approve_shows_the_proposal_and_approves_nothing(monkeypatch, quiet):
    """#806. Chat never showed the proposal before approving it, so the hash it pinned was read at
    the moment of approving and always matched. Now the first approve is the showing."""
    monkeypatch.setattr(server, "recent_runs",
                        lambda limit=25: [_run("only1", action="send_message", target="a@b.c")])
    monkeypatch.setattr(server, "run_detail", lambda rid: {"proposal_hash": "deadbeef99"})
    called = []
    monkeypatch.setattr(server.eng, "resume_run", lambda *a, **k: called.append(a))
    r = server._handle_gate_decision("approved", False)
    assert called == []
    assert "send_message" in r and "a@b.c" in r and "deadbeef" in r
    assert "Nothing is approved yet" in r


def test_the_second_approve_executes_the_version_shown(monkeypatch, quiet):
    monkeypatch.setattr(server, "recent_runs", lambda limit=25: [_run("only1")])
    monkeypatch.setattr(server, "run_detail", lambda rid: {"proposal_hash": "deadbeef"})
    seen = {}
    monkeypatch.setattr(server.eng, "resume_run",
                        lambda rid, d, h=None: seen.update(rid=rid, d=d, h=h) or
                        {"status": "executed", "outcome": "sent"})
    _, r = _approve_twice()
    assert seen == {"rid": "only1", "d": "approved", "h": "deadbeef"}
    assert "Approved 1 item" in r


def test_a_proposal_that_changed_after_it_was_shown_is_refused(monkeypatch, quiet):
    """The bug itself. The proposal is rewritten between the showing and the approval. The hash
    sent is the one shown, not the one stored now, so the engine's check has something to catch."""
    monkeypatch.setattr(server, "recent_runs", lambda limit=25: [_run("only1")])
    stored = {"h": "old-hash"}
    monkeypatch.setattr(server, "run_detail", lambda rid: {"proposal_hash": stored["h"]})
    sent = []

    def resume(rid, d, h=None):
        sent.append(h)
        if h != stored["h"]:
            return {"error": "proposal changed since it was shown; review it again before approving"}
        return {"status": "executed"}
    monkeypatch.setattr(server.eng, "resume_run", resume)
    server._handle_gate_decision("approved", False)
    stored["h"] = "new-hash"
    r = server._handle_gate_decision("approved", False)
    assert sent == ["old-hash"]
    assert "changed after I showed it" in r and "executed" not in r
    # The pin is spent, so the next approve shows the new version instead of retrying the old one.
    r = server._handle_gate_decision("approved", False)
    assert sent == ["old-hash"] and "new-hash"[:8] in r


def test_a_pin_older_than_half_an_hour_is_shown_again(monkeypatch, quiet):
    monkeypatch.setattr(server, "recent_runs", lambda limit=25: [_run("only1")])
    monkeypatch.setattr(server, "run_detail", lambda rid: {"proposal_hash": "h1"})
    called = []
    monkeypatch.setattr(server.eng, "resume_run", lambda *a, **k: called.append(a))
    server._handle_gate_decision("approved", False)
    server._CHAT_SHOWN["only1"] = ("h1", server.time.time() - server.CHAT_PIN_SECS - 1)
    r = server._handle_gate_decision("approved", False)
    assert called == [] and "Nothing is approved yet" in r


def test_several_pending_runs_are_named_rather_than_guessed_at(monkeypatch, quiet):
    """The deliberate change from the console, which took the newest silently. Picking one for the
    operator is the shape this project keeps paying for: they believe they approved the run they
    had in mind and the gate approved a different one."""
    monkeypatch.setattr(server, "recent_runs",
                        lambda limit=25: [_run("aaa", action="send_message", target="a@b.c"),
                                          _run("bbb", action="dispatch_build", target="nomad")])
    called = []
    monkeypatch.setattr(server.eng, "resume_run", lambda *a, **k: called.append(a))
    r = server._handle_gate_decision("approved", False)
    assert called == []
    assert "aaa" in r and "bbb" in r
    assert "approve all" in r


def test_approve_all_shows_them_all_then_approves_them_all(monkeypatch, quiet):
    monkeypatch.setattr(server, "recent_runs", lambda limit=25: [_run("aaa"), _run("bbb")])
    monkeypatch.setattr(server, "run_detail", lambda rid: {"proposal_hash": "h-" + rid})
    seen = []
    monkeypatch.setattr(server.eng, "resume_run",
                        lambda rid, d, h=None: seen.append((rid, h)) or {"status": "executed"})
    first, r = _approve_twice(do_all=True)
    assert "aaa" in first and "bbb" in first and "approve all" in first
    assert seen == [("aaa", "h-aaa"), ("bbb", "h-bbb")]
    assert "Approved 2 item" in r


def test_approve_all_with_one_unseen_run_approves_none(monkeypatch, quiet):
    """A run that arrived after the showing has not been seen, so nothing goes, not even the
    runs that were."""
    runs = [_run("aaa")]
    monkeypatch.setattr(server, "recent_runs", lambda limit=25: list(runs))
    monkeypatch.setattr(server, "run_detail", lambda rid: {"proposal_hash": "h-" + rid})
    called = []
    monkeypatch.setattr(server.eng, "resume_run", lambda *a, **k: called.append(a))
    server._handle_gate_decision("approved", True)
    runs.insert(0, _run("ccc"))
    r = server._handle_gate_decision("approved", True)
    assert called == [] and "ccc" in r and "Nothing is approved yet" in r


def test_a_run_whose_proposal_cannot_be_read_is_not_approved(monkeypatch, quiet):
    """No hash, no approval. Approving without one would be approving whatever the payload says
    at the moment the engine reads it, which is not what the operator was shown."""
    monkeypatch.setattr(server, "recent_runs", lambda limit=25: [_run("only1")])
    monkeypatch.setattr(server, "run_detail", lambda rid: {})
    called = []
    monkeypatch.setattr(server.eng, "resume_run", lambda *a, **k: called.append(a))
    for r in _approve_twice():
        assert "not approving" in r
    assert called == []


def test_a_rejection_needs_no_hash(monkeypatch, quiet):
    """Rejecting is the safe direction: the worst case is a run that has to be captured again.
    It also needs no showing first."""
    monkeypatch.setattr(server, "recent_runs", lambda limit=25: [_run("only1")])
    monkeypatch.setattr(server, "run_detail",
                        lambda rid: (_ for _ in ()).throw(AssertionError("must not be read")))
    monkeypatch.setattr(server.eng, "resume_run",
                        lambda rid, d, h=None: {"status": "declined"} if h is None else 1 / 0)
    assert "Rejected 1 item" in server._handle_gate_decision("rejected", False)


def test_a_resume_that_throws_is_reported_per_run(monkeypatch, quiet):
    monkeypatch.setattr(server, "recent_runs", lambda limit=25: [_run("only1")])
    monkeypatch.setattr(server, "run_detail", lambda rid: {"proposal_hash": "h"})
    monkeypatch.setattr(server.eng, "resume_run",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hash mismatch")))
    assert "hash mismatch" in _approve_twice()[1]


def test_only_runs_at_the_gate_are_candidates(monkeypatch, quiet):
    monkeypatch.setattr(server, "recent_runs",
                        lambda limit=25: [_run("done1", "executed"), _run("live1")])
    monkeypatch.setattr(server, "run_detail", lambda rid: {"proposal_hash": "h"})
    seen = []
    monkeypatch.setattr(server.eng, "resume_run",
                        lambda rid, d, h=None: seen.append(rid) or {"status": "executed"})
    _approve_twice()
    assert seen == ["live1"]


# ── the backlog ─────────────────────────────────────────────────────────────────────────────────

def test_the_backlog_sweep_says_nothing_executed(monkeypatch, quiet):
    """The console fired this at v1 crew, which did the work and left uncommitted edits with
    nothing in front of it. Every run this starts stops at the gate, and the reply says so."""
    monkeypatch.setattr(server.eng, "run_backlog",
                        lambda n: {"started": 1, "limit": 3, "skipped": [],
                                   "runs": [{"project": "NOMAD", "task_title": "T9",
                                             "run_id": "r9"}]})
    r = server._handle_self_develop()
    assert "awaiting approval" in r and "nothing has executed" in r
    assert "r9" in r


def test_an_empty_backlog_says_how_to_fill_it(monkeypatch, quiet):
    monkeypatch.setattr(server.eng, "run_backlog",
                        lambda n: {"started": 0, "limit": 3, "skipped": [], "runs": []})
    r = server._handle_self_develop()
    assert "Backlog is empty" in r and "Status = Backlog" in r


def test_a_backlog_where_everything_errored_is_not_reported_as_empty(monkeypatch, quiet):
    """Nothing started for two very different reasons, and they need different things done."""
    monkeypatch.setattr(server.eng, "run_backlog",
                        lambda n: {"started": 0, "limit": 3, "runs": [],
                                   "skipped": [{"goal_id": 4, "error": "no tasks table"}]})
    r = server._handle_self_develop()
    assert "Backlog is empty" not in r
    assert "no tasks table" in r


# ── starting a project ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def dispatcher(monkeypatch):
    """Stand in for the dispatcher's /new-project, which scaffolds a git repo on the host."""
    state = {"sent": None, "reply": {"ok": True, "name": "Aurora", "path": "/home/j/Aurora"}}

    class R:
        def __init__(self, body):
            self._b = body

        def read(self):
            return server.json.dumps(self._b).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        state["sent"] = req
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return R(state["reply"])
    monkeypatch.setattr(server.urllib.request, "urlopen", fake)
    return state


def test_a_project_is_scaffolded_then_planned(monkeypatch, dispatcher, quiet):
    monkeypatch.setattr(server.eng, "plan_project",
                        lambda t, d="": {"goal_id": 7, "milestones": [1, 2], "task_count": 9})
    r = server._handle_start_project("Aurora", "track solar output")
    assert "Aurora" in r and "/home/j/Aurora" in r
    assert "goal 7" in r and "2 milestones" in r and "9 tasks" in r


def test_the_dispatcher_call_is_authenticated(monkeypatch, dispatcher, quiet):
    """Every dispatcher route but /health needs the token, and a 403 here would otherwise read
    as "the project could not be created" with no hint why."""
    monkeypatch.setattr(server.eng, "plan_project", lambda *a, **k: {"goal_id": 1,
                                                                     "milestones": [],
                                                                     "task_count": 0})
    server._handle_start_project("Aurora", "x")
    assert dispatcher["sent"].get_header("X-nomad-token") == server.DISPATCH_TOKEN


def test_a_failed_plan_does_not_read_as_a_failed_scaffold(monkeypatch, dispatcher, quiet):
    """The repo exists either way. Reporting the whole thing as failed would send the operator
    looking for a directory that is already there."""
    monkeypatch.setattr(server.eng, "plan_project",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("goals table missing")))
    r = server._handle_start_project("Aurora", "x")
    assert "is live" in r
    assert "not on the board yet" in r and "goals table missing" in r


def test_an_unreachable_dispatcher_is_reported_as_such(dispatcher, quiet):
    dispatcher["reply"] = OSError("connection refused")
    r = server._handle_start_project("Aurora", "x")
    assert "Could not reach the dispatcher" in r


def test_a_project_with_no_name_is_asked_for_one(quiet):
    assert "I need a name" in server._handle_start_project("", "some request")


def test_nothing_here_writes_to_notion(monkeypatch, dispatcher, quiet):
    """The console filed the new project as a Notion row. Notion is the rollback source now and
    writing to it would make it stop being one."""
    import ast
    import inspect
    fn = ast.parse(textwrap.dedent(inspect.getsource(server._handle_start_project))).body[0]
    stmts = fn.body[1:] if ast.get_docstring(fn) else fn.body   # the docstring says why, so skip it
    code = "\n".join(ast.unparse(n) for n in stmts).lower()
    assert "notion" not in code


# ── the detector proxy ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def detector(monkeypatch):
    state = {"url": None, "payload": None, "reply": {"verdict": "human", "score": 0.2}}

    class R:
        status = 200

        def read(self):
            return server.json.dumps(state["reply"]).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        state["url"] = req.full_url
        state["payload"] = server.json.loads(req.data)
        state["timeout"] = timeout
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return R()
    monkeypatch.setattr(server.urllib.request, "urlopen", fake)
    return state


def test_the_full_check_goes_to_detect(detector):
    status, out = server._detect("some prose", lint=False, external=False)
    assert status == 200 and out["verdict"] == "human"
    assert detector["url"].endswith("/detect")
    assert detector["payload"] == {"text": "some prose", "external": False}


def test_the_style_pass_goes_to_lint_and_carries_no_external_flag(detector):
    """`/lint` is local and instant. Sending `external` to it would offer a paid provider a route
    it does not have, which is the kind of thing that only shows up on a bill."""
    server._detect("some prose", lint=True, external=True)
    assert detector["url"].endswith("/lint")
    assert detector["payload"] == {"text": "some prose"}


def test_external_is_off_unless_it_is_asked_for(detector):
    server._detect("x", lint=False, external=False)
    assert detector["payload"]["external"] is False
    server._detect("x", lint=False, external=True)
    assert detector["payload"]["external"] is True


def test_the_detector_stays_on_the_host_loopback(detector):
    """nomad-detect is bound to localhost on purpose and a container reaches it through the
    Docker gateway. A default pointing anywhere else would widen it by accident."""
    server._detect("x", lint=False, external=False)
    assert detector["url"].startswith("http://host.docker.internal:8214")


def test_the_full_check_is_given_time_to_run(detector):
    """The statistical pass is slow. A few-second timeout would turn every real check into an
    unreachable detector."""
    server._detect("x", lint=False, external=False)
    assert detector["timeout"] >= 120


def test_an_unreachable_detector_is_a_503(detector):
    detector["reply"] = OSError("connection refused")
    status, out = server._detect("x", lint=False, external=False)
    assert status == 503
    assert "unreachable" in out["error"]


def test_the_detectors_own_refusal_is_passed_through_not_rewritten(monkeypatch):
    """A service that is up and declining needs something different done about it than one that is
    down, so its status code has to survive the proxy."""
    def fake(req, timeout=None):
        raise server.urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, None)
    monkeypatch.setattr(server.urllib.request, "urlopen", fake)
    status, out = server._detect("x", lint=False, external=False)
    assert status == 422
    assert "422" in out["error"]
