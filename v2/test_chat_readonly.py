"""#971 · the cockpit chat stays read-only, answers from its tools, and is the same over the tailnet.

Three claims (PRD docs/prd/cockpit-chat-tools.md 3, 4 and 8):
1. Every tool the chat can reach is enumerated, run against a board where each write is recorded,
   and the set fails the check the moment a write-capable tool joins it.
2. Each row of the question set (Q1 to Q22) goes in through `chat_reply`, and the answer carries a
   fact that only the named tool's data holds: the facts are in the fake board, never in the prompt.
3. A chat that arrives through `tailscale serve` gets the same allowlist as the laptop, and a
   request to write is refused or comes to nothing on both.

The SDK is the fake from test_chat_reply_tools.py. Its "model" is a script that calls the tools a
question names and then says only what it read, so what is checked is NOMAD's side: which tools
are reachable, that they run on live data, and that nothing they do writes.

Run: python3 -m pytest v2/test_chat_readonly.py -q   (engine deps live in the container)
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import requests  # noqa: E402
import server  # noqa: E402
import runtime  # noqa: E402
import chat_tools  # noqa: E402
import chat_pm_tools  # noqa: E402
import memory_chat  # noqa: E402
from test_chat_tools import (COMMS, EPISODIC, SUP_STATUS, SUP_CONFIG, SUP_RUNS, TELEMETRY,  # noqa: E402
                             FAILED, GATED, DONE)
from test_chat_pm_tools import FakeOP  # noqa: E402
from test_chat_reply_tools import (sdk, chat, ResultMessage, PermissionResultAllow,  # noqa: E402,F401
                                   PermissionResultDeny, _say)

CLASSIFY_MEMORY = memory_chat.classify_memory_intent

# Verbs a tool name must not carry. A name check on top of the behaviour check below.
WRITE_VERBS = ("create", "update", "delete", "remove", "write", "edit", "send", "post", "put",
               "patch", "comment", "transition", "move", "relate", "watch", "approve", "reject",
               "retry", "resume", "capture", "dispatch", "start", "stop", "set", "plan",
               "forget", "remember", "trigger", "execute")

# Representative arguments, so each tool's handler actually reads rather than stopping at a check.
ARGS = {"pipeline_runs": {"status": "failed"}, "pipeline_run_detail": {"run_id": FAILED},
        "autopilot_runs": {"project": "nomad"}, "pm_search": {"text": "phone"},
        "pm_get": {"id": "969"}, "pm_tree": {"id": "965"}, "pm_ready": {"project": "nomad-work"},
        "pm_project_summary": {"project": "nomad-work"}}

# Names the model could ask for that must be refused: every built-in, the nomad-pm MCP writes and
# the engine's acting endpoints, and a name nobody has thought of yet.
NOT_REACHABLE = list(runtime.BUILTIN_TOOLS) + [
    "mcp__nomad-pm__create", "mcp__nomad-pm__create_plan", "mcp__nomad-pm__transition",
    "mcp__nomad-pm__comment", "mcp__nomad-pm__update_fields", "mcp__nomad-pm__relate",
    "mcp__nomad-pm__unrelate", "mcp__nomad-pm__watch", "mcp__nomad-chat__pipeline_retry",
    "mcp__nomad-chat__approve_run", "mcp__nomad-chat__capture", "mcp__nomad-chat__pm_transition",
    "mcp__nomad-chat__dispatch", "mcp__nomad-chat__", "mcp__nomad__resume", "approve", "*"]


# ── a live board where every write is recorded ──────────────────────────────────────────────────

@pytest.fixture
def live(monkeypatch, tmp_path):
    """NocoDB, the supervisor, service health and OpenProject, each a fake with known data. Every
    write any of them could take is recorded in `writes` and then raises, so a tool that writes
    shows up here even when its own error handling swallows the exception."""
    writes = []

    def record(what):
        def f(*a, **k):
            writes.append(what)
            raise AssertionError(f"write reached: {what}")
        return f

    def fake_list(table, limit=50, where=None, sort=None):
        return COMMS[:limit] if table == "comms" else EPISODIC if table == "episodic" else []

    def fake_find(table, field, value):
        return next((r for r in COMMS if r.get(field) == value), None) if table == "comms" else None

    monkeypatch.setattr(server.db, "list", fake_list, raising=False)
    monkeypatch.setattr(server.db, "find", fake_find, raising=False)
    for name in ("create", "update", "delete", "create_table", "_req"):
        monkeypatch.setattr(server.db, name, record(f"db.{name}"), raising=False)

    def fake_autopilot(method, path, body=None):
        if method != "GET":
            return record(f"supervisor {method} {path}")()
        if path == "/status":
            return 200, SUP_STATUS
        if path == "/config":
            return 200, SUP_CONFIG
        if path.startswith("/runs"):
            return 200, {"ok": True, "runs": SUP_RUNS[:int(path.split("limit=")[1])]}
        return 404, {"ok": False, "error": "not found"}
    monkeypatch.setattr(server, "_autopilot", fake_autopilot)

    for name in ("start_run", "resume_run", "plan_project", "retry_run", "dispatch"):
        monkeypatch.setattr(server.eng, name, record(f"eng.{name}"), raising=False)
    for name in ("_handle_gate_decision", "_handle_pipeline_capture"):
        monkeypatch.setattr(server, name, record(name))
    monkeypatch.setattr(memory_chat, "forget", record("memory_chat.forget"))
    # `chat` stubs the memory-command classifier; the real one is what sees "forget everything".
    monkeypatch.setattr(memory_chat, "classify_memory_intent", CLASSIFY_MEMORY)

    watch = tmp_path / "state.json"
    watch.write_text(json.dumps({
        "_meta": {"v": 1},
        "litellm": {"ok": True, "detail": "200", "checked": "2026-10-01T10:00:00"},
        "qdrant": {"ok": False, "detail": "connection refused", "fails": 3,
                   "since": "2026-10-01T09:00:00", "checked": "2026-10-01T10:00:00"}}))
    monkeypatch.setattr(server, "WATCH_STATE", str(watch))
    monkeypatch.setattr(server, "_proc_telemetry", lambda: TELEMETRY)

    op = FakeOP()

    def op_request(method, url, *a, **k):
        if method.upper() == "GET":
            return op.get(url)
        return record(f"OpenProject {method} {url}")()
    monkeypatch.setattr(requests, "get", op.get)
    monkeypatch.setattr(requests, "request", op_request)
    for m in ("post", "put", "patch", "delete"):
        monkeypatch.setattr(requests, m, lambda url, *a, _m=m, **k: op_request(_m, url))
    monkeypatch.setattr(chat_pm_tools, "_base", "http://op.test")
    monkeypatch.setattr(chat_pm_tools, "_tokens", {"nomad-chat": "tok"})
    monkeypatch.setattr(chat_pm_tools, "_cache", {})
    monkeypatch.setattr(chat_pm_tools, "OPERATOR_ID", 4)
    return writes


def write_capable(tools: dict, writes: list) -> dict:
    """The check itself: each reachable tool, by name and by what it does when run. Returns
    {tool: why} for every one that can write; empty means the set is read-only."""
    bad = {}
    for name, spec in tools.items():
        verbs = [v for v in WRITE_VERBS if v in name.lower().split("_")]
        if verbs:
            bad[name] = f"its name says {verbs[0]}"
            continue
        before = len(writes)
        spec["call"](ARGS.get(name, {}))
        if len(writes) > before:
            bad[name] = f"running it wrote: {writes[before:]}"
    return bad


def reachable(sdk_state) -> dict:
    """Every tool a chat turn can reach, read off what the SDK was handed: the allowlist, the tools
    registered on the MCP servers, and whatever the permission gate lets through."""
    o = sdk_state["options"]
    allowed = set(o.allowed_tools)
    registered = {f"mcp__{srv}__{t['name']}" for srv, s in o.mcp_servers.items() for t in s["tools"]}
    assert registered == allowed, (registered, allowed)
    assert not any("*" in n for n in allowed)
    return {n.removeprefix(runtime.tool_name("")): n for n in allowed}


def _ok(sdk_state):
    async def script(client):
        yield _say("ok")
        yield ResultMessage()
    sdk_state["script"] = script


def _one_turn(sdk_state, message="anything running?"):
    _ok(sdk_state)
    return server.chat_reply(message)


# ── 1. the allowlist test ───────────────────────────────────────────────────────────────────────

def test_every_tool_the_chat_can_reach_is_read_only(sdk, chat, live):
    _one_turn(sdk)
    names = reachable(sdk)
    assert set(names) == set(server.CHAT_TOOL_ALLOWLIST)
    tools = server._chat_tools()
    assert set(tools) == set(names)
    assert write_capable(tools, live) == {}
    assert live == []


def test_the_permission_gate_lets_nothing_else_through(sdk, chat, live):
    seen = {}

    async def script(client):
        gate = sdk["options"].can_use_tool
        for n in NOT_REACHABLE + list(sdk["options"].allowed_tools):
            seen[n] = await gate(n, {}, None)
        yield _say("ok")
        yield ResultMessage()
    sdk["script"] = script
    server.chat_reply("anything running?")
    allowed = {n for n, r in seen.items() if isinstance(r, PermissionResultAllow)}
    assert allowed == set(sdk["options"].allowed_tools)
    assert all(isinstance(seen[n], PermissionResultDeny) for n in NOT_REACHABLE)
    assert set(runtime.BUILTIN_TOOLS) <= set(sdk["options"].disallowed_tools)
    assert sdk["options"].tools == [] and sdk["options"].setting_sources == []


def _add(monkeypatch, name, handler):
    monkeypatch.setitem(chat_tools.TOOLS, name, {"name": name, "read_only": True, "handler": handler,
                                                 "description": "x", "input_schema": chat_tools._NONE})
    monkeypatch.setattr(server, "CHAT_TOOL_ALLOWLIST", server.CHAT_TOOL_ALLOWLIST + (name,))


def test_a_write_tool_added_to_the_set_fails_the_check_by_name(sdk, chat, live, monkeypatch):
    """Someone names `pipeline_retry` on the allowlist and marks it read-only: the check catches it."""
    _add(monkeypatch, "pipeline_retry", lambda: {"ok": True})
    _one_turn(sdk)
    assert "pipeline_retry" in reachable(sdk)
    assert set(write_capable(server._chat_tools(), live)) == {"pipeline_retry"}


@pytest.mark.parametrize("does", [
    lambda: server.db.create("comms", {"status": "approved"}),
    lambda: server._autopilot("POST", "/pass"),
    lambda: server.eng.start_run("a goal"),
    lambda: requests.post("http://op.test/api/v3/work_packages/969/activities", json={}),
])
def test_a_write_tool_with_a_harmless_name_fails_the_check_by_what_it_does(sdk, chat, live,
                                                                          monkeypatch, does):
    """A tool called `pipeline_peek` that writes through NocoDB, the supervisor, the engine or
    OpenProject is caught when it runs, even though its own error handling hides the failure."""
    _add(monkeypatch, "pipeline_peek", lambda: does() or {})
    _one_turn(sdk)
    assert "pipeline_peek" in reachable(sdk)
    bad = write_capable(server._chat_tools(), live)
    assert set(bad) == {"pipeline_peek"} and "wrote" in bad["pipeline_peek"]


# ── 2. the question set, answered from tool data ────────────────────────────────────────────────
#
# Each row: the question, then (tool, arguments, fact) for each tool PRD 3 names. The fact is a
# value from the fake board that the question doesn't contain and the prompt doesn't carry, so an
# answer holding it was read through that tool.

QUESTIONS = [
    ("Q1", "What's running in the pipeline right now?",
     [("pipeline_runs", {}, GATED)]),
    ("Q2", "Show me the last 10 runs.",
     [("pipeline_runs", {"limit": 10}, DONE)]),
    ("Q3", "Why did run 25693e fail?",
     [("pipeline_run_detail", {"run_id": "25693e"}, "SMTP 535 auth failed")]),
    ("Q4", f"What did run {GATED[:6]} propose, and what did it actually file or send?",
     [("pipeline_run_detail", {"run_id": GATED}, "mail the brief")]),
    ("Q5", "Show me the log for run 25693e.",
     [("pipeline_run_detail", {"run_id": "25693e"}, "step 49")]),
    ("Q6", "What's waiting for my approval, and why?",
     [("approval_queue", {}, "Here is the brief.")]),
    ("Q7", "Is anything at the gate older than a day?",
     [("approval_queue", {}, GATED)]),
    ("Q8", "What has autopilot run today, and how did each run end?",
     [("autopilot_runs", {}, "autopilot: result ready to merge")]),
    ("Q9", "Which projects is autopilot working on right now?",
     [("autopilot_status", {}, "runematch")]),
    ("Q10", "Which projects need me (Awaiting approval, failed, budget paused)?",
     [("autopilot_status", {}, "needs you"),
      ("pm_search", {"status": "In Progress"}, "Read-only nomad-pm access")]),
    ("Q11", "Is autopilot on for RuneMatch? What's the budget mode and the spend today?",
     [("autopilot_status", {}, "3.5"), ("autopilot_config", {}, '"daily_cap": 20')]),
    ("Q12", "What did the worker say about task #969?",
     [("autopilot_runs", {"project": "nomad"}, "feat/968"), ("pm_get", {"id": "969"}, "looks right")]),
    ("Q13", "What's the status of #969? Who's on it, and what's blocking it?",
     [("pm_get", {"id": "969"}, "Josias")]),
    ("Q14", "Find the tickets about the cockpit phone layout.",
     [("pm_search", {"text": "phone"}, "#972")]),
    ("Q15", "Show me the tree under feature #965.",
     [("pm_tree", {"id": "965"}, "Test the question set")]),
    ("Q16", "What's ready to pick up in NOMAD work?",
     [("pm_ready", {"project": "nomad-work"}, "Test the question set")]),
    ("Q17", "Give me a summary of the NOMAD work board.",
     [("pm_project_summary", {"project": "nomad-work"}, "Cockpit phone layout overflows")]),
    ("Q18", "What's on my plate?",
     [("pm_my_queue", {}, "Read-only nomad-pm access")]),
    ("Q19", "Are all services up? Which one is down, and since when?",
     [("system_health", {}, "2026-10-01T09:00:00")]),
    ("Q20", "How's the box doing (CPU, RAM, GPU, sweep heartbeat)?",
     [("system_health", {}, "RTX 5090")]),
]


def _answer_from(sdk_state, calls):
    """The scripted model: call each tool, then say only the facts its results actually hold."""
    async def script(client):
        said = []
        for name, args, fact in calls:
            res = await client.call(name, args)
            if fact in res["content"][0]["text"]:
                said.append(fact)
        yield _say(" · ".join(said) or "I found nothing.")
        yield ResultMessage(num_turns=len(calls) + 1)
    sdk_state["script"] = script


@pytest.mark.parametrize("qid,question,calls", QUESTIONS, ids=[q[0] for q in QUESTIONS])
def test_each_question_is_answered_from_its_tools(sdk, chat, live, qid, question, calls):
    _answer_from(sdk, calls)
    out = server.chat_reply(question)
    assert out["tools"] == [name for name, _, _ in calls]
    assert chat["fallback"] == []                               # the tool turn answered, not the snapshot
    for name, _, fact in calls:
        assert fact in out["reply"], (qid, name, fact, out["reply"])
        assert fact not in question and fact not in sdk["options"].system_prompt
    assert live == []


def test_the_question_set_covers_every_tool_family():
    """PRD 3 / parent epic #966: runs and run detail, the approval queue, autopilot runs and
    projects, service health and the nomad-pm queries. Q21 and Q22 are below."""
    used = {name for _, _, calls in QUESTIONS for name, _, _ in calls}
    assert used == set(server.CHAT_TOOL_ALLOWLIST)
    assert [q[0] for q in QUESTIONS] == [f"Q{i}" for i in range(1, 21)]


@pytest.mark.parametrize("question", ["Run diagnostics.", "Sitrep."])
def test_q21_diagnostics_reports_the_live_board(sdk, chat, live, question):
    """Q21 still goes through the diagnostics handler (#973 keeps it); it reads the same runs and
    telemetry the tools do, and the model is never asked."""
    out = server.chat_reply(question)
    assert out.get("intent") == "diagnostics"
    assert "1 failed" in out["reply"] and "1 awaiting-approval" in out["reply"]
    assert "RTX 5090" in out["reply"]
    assert sdk["options"] is None and chat["fallback"] == [] and live == []


def test_q22_send_that_email_gets_the_pointer_and_acts_on_nothing(sdk, chat, live):
    out = server.chat_reply("Send that email.")
    assert out["reply"] == server.CHAT_POINTER and out.get("refused")
    assert sdk["options"] is None and live == []


def test_q22_can_you_approve_has_no_tool_that_could(sdk, chat, live):
    """"Can you approve run 3fa9c1?" isn't a command the router knows, so the model gets it. All it
    could reach is read-only: asking for an approve or a transition is refused, and nothing writes."""
    tried = {}

    async def script(client):
        gate = sdk["options"].can_use_tool
        for n in ("mcp__nomad-chat__approve_run", "mcp__nomad-pm__transition", "Bash"):
            tried[n] = await gate(n, {"run_id": "3fa9c1"}, None)
        yield _say(server.CHAT_POINTER)
        yield ResultMessage()
    sdk["script"] = script
    out = server.chat_reply("Can you approve run 3fa9c1?")
    assert all(isinstance(r, PermissionResultDeny) for r in tried.values())
    assert out["tools"] == [] and live == []
    assert not any("approve" in n for n in sdk["options"].allowed_tools)


# ── 3. the remote (tailnet) chat ────────────────────────────────────────────────────────────────

OP_LOGIN = "jde@example.com"
ORIGIN = "https://nomad.tail1234.ts.net:8449"


@pytest.fixture
def tailnet(monkeypatch):
    monkeypatch.setattr(server, "AUTH_MODE", "tailnet")
    monkeypatch.setattr(server, "TAILNET_OPERATOR", OP_LOGIN)
    monkeypatch.setattr(server, "TAILNET_ORIGIN", ORIGIN)


def _post(body, remote):
    from starlette.testclient import TestClient
    headers = ({"x-forwarded-for": "100.64.0.7", "tailscale-user-login": OP_LOGIN, "origin": ORIGIN}
               if remote else {})
    r = TestClient(server.app).post("/chat", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_remote_question_is_answered_from_the_same_tools(sdk, chat, live, tailnet):
    _, question, calls = QUESTIONS[2]                                   # Q3
    _answer_from(sdk, calls)
    remote = _post({"message": question}, remote=True)
    remote_opts = sdk["options"]
    _answer_from(sdk, calls)
    local = _post({"message": question}, remote=False)
    assert remote["reply"] == local["reply"] == "SMTP 535 auth failed"
    assert remote["tools"] == local["tools"] == ["pipeline_run_detail"]
    assert sorted(remote_opts.allowed_tools) == sorted(sdk["options"].allowed_tools)
    assert [t["name"] for t in remote_opts.mcp_servers["nomad-chat"]["tools"]] == \
           [t["name"] for t in sdk["options"].mcp_servers["nomad-chat"]["tools"]]
    assert live == []


def test_the_remote_tool_turn_has_the_same_allowlist_and_is_read_only(sdk, chat, live, tailnet):
    _ok(sdk)
    _post({"message": "anything running?"}, remote=True)
    names = reachable(sdk)
    assert set(names) == set(server.CHAT_TOOL_ALLOWLIST)
    assert write_capable(server._chat_tools(), live) == {}
    assert live == []


WRITE_ASKS = [
    {"message": "approve"}, {"message": "approve all"}, {"message": "reject"},
    {"message": "approve", "voice": True}, {"message": "Send that email."},
    {"message": "/capture mail the brief"}, {"message": "capture: fix the relay"},
    {"message": "⚑ fix the relay"}, {"message": "fix the relay", "capture": True},
    {"message": "project: a new thing"}, {"message": "start a new project called Atlas"},
    {"message": "work on your backlog"}, {"message": "research local STT engines"},
    {"message": "forget everything"}, {"message": "forget that"},
]


@pytest.mark.parametrize("body", WRITE_ASKS, ids=[b["message"] + (" (voice)" if b.get("voice") else "")
                                                  + (" (capture)" if b.get("capture") else "")
                                                  for b in WRITE_ASKS])
def test_a_remote_write_request_is_refused_and_acts_on_nothing(sdk, chat, live, tailnet, body):
    out = _post(body, remote=True)
    assert out.get("refused") and out["reply"] == server.REMOTE_REFUSAL
    assert sdk["options"] is None                                      # the model never saw it
    assert live == []


@pytest.mark.parametrize("body", [b for b in WRITE_ASKS if not b.get("voice")
                                  and not b["message"].startswith("forget")],
                         ids=lambda b: b["message"])
def test_the_typed_box_on_the_laptop_refuses_the_same_requests(sdk, chat, live, tailnet, body):
    """The same asks from the laptop's typed box: the pointer, never the model, nothing written.
    (Voice on the laptop and the forget commands act there on purpose, #973.)"""
    out = _post(body, remote=False)
    assert out.get("refused") and out["reply"] == server.CHAT_POINTER
    assert sdk["options"] is None and live == []


def test_a_write_the_model_asks_for_is_denied_the_same_way_remote_and_local(sdk, chat, live, tailnet):
    """A question that reaches the model on either path: the gate's answer to a write is identical."""
    answers = {}

    def ask(where):
        async def script(client):
            gate = sdk["options"].can_use_tool
            answers[where] = {n: await gate(n, {}, None) for n in NOT_REACHABLE}
            yield _say("I can only read.")
            yield ResultMessage()
        sdk["script"] = script
    ask("remote")
    remote = _post({"message": "can you mark #969 done?"}, remote=True)
    ask("local")
    local = _post({"message": "can you mark #969 done?"}, remote=False)
    assert remote["reply"] == local["reply"] == "I can only read."
    assert answers["remote"] == answers["local"]
    assert all(isinstance(r, PermissionResultDeny) for r in answers["remote"].values())
    assert remote["tools"] == local["tools"] == [] and live == []
