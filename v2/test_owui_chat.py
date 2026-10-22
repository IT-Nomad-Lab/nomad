"""#1117, #1134 · POST /owui/chat and /owui/context (docs/prd/owui-sessions.md §6.2, §6.5, N3, N4, U5).

Run: python3 -m pytest v2/test_owui_chat.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import owui_chat as O  # noqa: E402
import owui_context  # noqa: E402
import owui_router as R  # noqa: E402

# LiteLLM's chain today (litellm/config.yaml); the fake follows it unless the body brings its own.
CHAIN = {"deep": ["balanced", "codex", "fast"], "balanced": ["codex", "fast"],
         "codex": ["balanced", "fast"], "fast": ["private"],
         "code": ["code-ollama", "codex", "fast"], "vision": ["vision-ollama"]}
ROWS = [{"id": "civis-poc", "name": "Civis"}, {"id": "notes", "name": "Notes", "local_only": True}]
CTX = {"id": "civis-poc", "name": "Civis", "board": "civis",
       "claude_md": {"path": "CLAUDE.md", "text": "# Civis rules"}, "prd": None}


class FakeLiteLLM:
    def __init__(self, down=(), answer="the answer"):
        self.down, self.answer, self.tried, self.bodies = set(down), answer, [], []

    def __call__(self, body):
        self.bodies.append(body)
        model = body["model"]
        chain = [] if body.get("disable_fallbacks") else body.get("fallbacks", CHAIN.get(model, []))
        for i, alias in enumerate([model] + chain):
            self.tried.append(alias)
            if alias not in self.down:
                return ({"X-LiteLLM-Model-Group": alias, "x-litellm-attempted-fallbacks": str(i)},
                        iter([self.answer[:4], self.answer[4:]]))
        raise RuntimeError("No deployments available")


class Bindings(dict):
    def bound(self, chat_id):
        return self.get(chat_id)

    def bind(self, chat_id, entry):
        self[chat_id] = entry


def deps(llm, router_reply=None, rows=ROWS, memory=None, log=None, bindings=None):
    def complete(alias, system, user, timeout):
        assert alias == "fast"
        if router_reply is None:
            raise TimeoutError
        return json.dumps(router_reply)
    mem = memory if memory is not None else []
    return O.Deps(registry=lambda: rows, fetch_context=lambda pid: CTX,
                  search=lambda **k: "#1 [Task] something", complete=complete, stream=llm,
                  recall=lambda q, s: [], remember=lambda s, r, c: mem.append((s, r, c)),
                  log=(log.append if log is not None else lambda row: None),
                  **({"bound": bindings.bound, "bind": bindings.bind} if bindings is not None else {}))


def run(req, d):
    events = list(O.turn(req, d))
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    return events, text


@pytest.fixture(autouse=True)
def fresh_cache():
    owui_context._cache.clear()


def req(entry, *texts, chat="c1"):
    msgs = []
    for i, t in enumerate(texts):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": t})
    return {"entry": entry, "chat_id": chat, "messages": msgs}


def test_a_project_turn_gets_context_and_the_served_footer():
    llm, mem, log = FakeLiteLLM(), [], []
    events, text = run(req("p-civis-poc", "what's next?"),
                       deps(llm, {"model": "balanced", "reason": "project status"}, memory=mem, log=log))
    assert [e["text"] for e in events if e["type"] == "status"] == ["routing…", "→ balanced"]
    assert text.startswith("the answer\n\n---\nrouted: balanced (why: project status) · served by: balanced")
    system = llm.bodies[0]["messages"][0]["content"]
    assert "PROJECT: Civis (registry id civis-poc, nomad-pm board civis)" in system
    assert "fallbacks" not in llm.bodies[0] and llm.bodies[0]["stream"] is True
    assert mem == [("owui:c1", "user", "what's next?"), ("owui:c1", "assistant", "the answer")]
    assert log[0]["routed"] == "balanced" and log[0]["served"] == "balanced"
    assert events[-1] == {"type": "done", "routed": "balanced", "served": "balanced",
                          "footer": text.split("---\n")[1], "entry": "p-civis-poc",
                          "name": "Civis"}


def test_u7_a_bare_override_reasks_without_recalled_memory():
    """#1180: recall fed the first answer back in, and the second model copied it."""
    llm, asked = FakeLiteLLM(), []
    d = deps(llm)
    d.recall = lambda q, s: asked.append(q) or [{"role": "assistant", "content": "the old answer"}]
    _, text = run(req("general", "the question", "the old answer", "!local"), d)
    body = llm.bodies[0]
    assert body["model"] == "private" and body["messages"][-1]["content"] == "the question"
    assert "RECALLED MEMORY" not in body["messages"][0]["content"] and asked == []
    assert "routed: private (why: !local)" in text
    run(req("general", "the question", "the old answer", "!local and be brief"), d)
    assert asked == ["and be brief"]


def test_u5_claude_throttled_is_answered_by_the_chain_and_the_footer_says_so():
    llm = FakeLiteLLM(down={"deep", "balanced"})
    _, text = run(req("p-civis-poc", "weigh splitting the engine"),
                  deps(llm, {"model": "deep", "reason": "architecture"}))
    assert llm.tried == ["deep", "balanced", "codex"]
    assert "routed: deep (why: architecture) · served by: codex (fallback after deep failed)" in text


@pytest.mark.parametrize("entry", ["p-notes"])
@pytest.mark.parametrize("router", [{"model": "code", "reason": "r"}, {"model": "deep", "reason": "r"},
                                    None])
def test_n3_local_only_chats_never_reach_a_cloud_alias(entry, router):
    llm = FakeLiteLLM(down=set(R.LOCAL) | {"code-ollama", "vision-ollama"})   # every local fails
    _, text = run(req(entry, "/deep think", "a", "q"), deps(llm, router))
    _, text = run(req(entry, "write me a parser"), deps(llm, router))
    assert llm.tried and not set(llm.tried) & set(R.CLOUD + R.PAID)
    assert "⚠ No model answered" in text and "served by: nothing" in text


def test_general_is_no_project_and_may_use_a_cloud_model():
    llm = FakeLiteLLM()
    events, text = run(req("general", "weigh this"), deps(llm, {"model": "deep", "reason": "hard"}))
    assert llm.bodies[0]["model"] == "deep" and "fallbacks" not in llm.bodies[0]
    assert O.GENERAL in llm.bodies[0]["messages"][0]["content"]
    assert "served by: deep" in text and events[-1]["entry"] == "general"


def test_a_project_chat_stays_local_while_the_registry_is_down():
    llm = FakeLiteLLM(down={"private", "fast"})
    _, text = run(req("p-civis-poc", "hi"), deps(llm, {"model": "fast", "reason": "r"}, rows=None))
    assert set(llm.tried) == {"fast", "private"}
    assert "registry unreachable, so this turn stays local" in text


def test_overrides_and_the_reask():
    llm = FakeLiteLLM()
    _, text = run(req("p-civis-poc", "the question", "a local answer", "/deep"), deps(llm))
    assert llm.bodies[0]["model"] == "deep"
    assert llm.bodies[0]["messages"][-1] == {"role": "user", "content": "the question"}
    assert llm.bodies[0]["messages"][1:3] == [{"role": "user", "content": "the question"},
                                              {"role": "assistant", "content": "a local answer"}]
    assert "routed: deep (why: /deep)" in text


def test_an_image_goes_to_vision_with_its_parts():
    llm = FakeLiteLLM()
    parts = [{"type": "text", "text": "what is this"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    run({"entry": "p-notes", "chat_id": "c", "messages": [{"role": "user", "content": parts}]},
        deps(llm))
    assert llm.bodies[0]["model"] == "vision" and llm.bodies[0]["messages"][-1]["content"] == parts
    assert llm.bodies[0]["fallbacks"] == ["vision-ollama"]


def test_router_down_says_so_in_the_footer():
    _, text = run(req("general", "hi"), deps(FakeLiteLLM()))
    assert "routed: balanced (why: default) · served by: balanced · router skipped" in text


def test_new_project_and_unknown_entries_answer_without_a_model():
    llm = FakeLiteLLM()
    _, text = run(req("new-project", "hi"), deps(llm))
    assert "**1/8.** What's the project called?" in text and "new project · interview" in text
    _, text = run(req("p-nope", "hi"), deps(llm))
    assert "No such NOMAD entry" in text and llm.bodies == []


def test_the_first_turn_binds_the_chat_and_later_turns_keep_it():
    b, llm = Bindings(), FakeLiteLLM()
    events, _ = run(req("p-civis-poc", "hi"), deps(llm, {"model": "fast", "reason": "r"}, bindings=b))
    assert b == {"c1": "p-civis-poc"} and events[-1]["entry"] == "p-civis-poc"
    assert events[-1]["name"] == "Civis"
    events, text = run(req("general", "hi"), deps(llm, {"model": "fast", "reason": "r"}, bindings=b))
    assert b == {"c1": "p-civis-poc"} and events[-1]["entry"] == "p-civis-poc"
    assert "this chat stays on p-civis-poc" in text and "# Civis rules" in llm.bodies[-1]["messages"][0]["content"]


def test_a_new_project_chat_is_bound_too_and_unknown_entries_never_bind():
    b = Bindings()
    events, _ = run(req("new-project", "hi"), deps(FakeLiteLLM(), bindings=b))
    assert b == {"c1": "new-project"} and events[-1]["entry"] == "new-project"
    b2 = Bindings()
    run(req("p-nope", "hi"), deps(FakeLiteLLM(), bindings=b2))
    run({**req("p-civis-poc", "t"), "task": "title_generation"}, deps(FakeLiteLLM(), bindings=b2))
    assert b2 == {}


def test_a_broken_binding_store_doesnt_stop_the_turn():
    def boom(*a):
        raise OSError("nocodb down")
    d = deps(FakeLiteLLM(), {"model": "fast", "reason": "r"})
    d.bound = boom
    events, text = run(req("p-civis-poc", "hi"), d)
    assert "project lock unavailable" in text and events[-1]["entry"] == "p-civis-poc"


def test_context_for_the_filter():
    b = Bindings()
    out = O.context({"chat_id": "c1", "entry": "p-civis-poc", "model": "gpt"}, deps(None, bindings=b))
    assert out["entry"] == "p-civis-poc" and out["system"].startswith(O.FILTER)
    assert "# Civis rules" in out["system"] and b == {"c1": "p-civis-poc"}
    out = O.context({"chat_id": "c1", "entry": "general", "model": "gpt"}, deps(None, bindings=b))
    assert out["entry"] == "p-civis-poc" and "stays on p-civis-poc" in out["note"]
    for entry in ("general", "new-project"):
        assert O.context({"chat_id": "c2" + entry, "entry": entry, "model": "deep"},
                         deps(None, bindings=Bindings()))["system"] == ""


def test_context_decides_a_model_override_for_the_filter():
    ctx = lambda entry, *users, model="codex": O.context(
        {"chat_id": "c" + entry, "entry": entry, "model": model,
         "messages": [{"role": "user", "content": u} for u in users]}, deps(None, bindings=Bindings()))
    out = ctx("general", "/deep why is this slow?")
    assert out["override"] == {"model": "deep", "cmd": "/deep", "text": "why is this slow?",
                               "refused": False, "note": ""}
    out = ctx("p-civis-poc", "what now", "!local")             # bare: re-asks the earlier question
    assert out["override"]["model"] == "private" and out["override"]["text"] == "what now"
    assert "override" not in ctx("general", "hello there")
    assert "override" not in ctx("general")
    assert "override" not in ctx("new-project", "/deep x")


def test_a_cloud_override_in_a_local_only_project_is_refused_without_its_context_going_out():
    out = O.context({"chat_id": "c", "entry": "p-notes", "model": "codex",
                     "messages": [{"role": "user", "content": "/deep why?"}]}, deps(None))
    assert out["override"] == {"model": "private", "cmd": "/deep", "text": "why?", "refused": True,
                               "note": "/deep is a cloud model; this chat stays local"}
    assert out["system"]                                       # private is local, so context is fine


def test_a_local_only_project_never_sends_its_context_to_a_cloud_model():
    for model, sent in (("deep", False), ("llama3.1:8b", False), ("private", True)):
        out = O.context({"chat_id": "c", "entry": "p-notes", "model": model}, deps(None))
        assert bool(out["system"]) is sent
        assert sent or "local-only" in out["note"]


def test_memory_and_log_failures_dont_break_the_turn():
    def boom(*a):
        raise OSError("qdrant down")
    d = deps(FakeLiteLLM(), {"model": "fast", "reason": "r"})
    d.recall = d.remember = d.log = boom
    events, text = run(req("general", "hi"), d)
    assert text.startswith("the answer") and events[-1]["type"] == "done"


def test_gpt_never_leaves_the_engine():
    with pytest.raises(AssertionError):
        O._body(R.Decision("gpt", "x", "router", "m"), [])


def test_open_webui_tasks_go_to_fast_bare():
    llm, mem, log = FakeLiteLLM(answer='{"title": "Hi"}'), [], []
    r = {**req("p-civis-poc", "make a title"), "task": "title_generation"}
    events, text = run(r, deps(llm, {"model": "deep", "reason": "r"}, memory=mem, log=log))
    assert text == '{"title": "Hi"}' and "routed:" not in text
    assert llm.bodies[0]["model"] == "fast" and llm.bodies[0]["fallbacks"] == ["private"]
    assert mem == [] and log == [] and len(llm.bodies[0]["messages"]) == 1


# ── tools (M2, #1147) ──
import dataclasses  # noqa: E402

import owui_tools  # noqa: E402
from test_owui_tools import FakeBoardOP  # noqa: E402

BOARD_ROWS = [{"id": "civis-poc", "name": "Civis", "board": "civis"},
              {"id": "notes", "name": "Notes", "local_only": True, "board": "notes"}]


class Scripted:
    """LiteLLM tool calling, scripted: each call returns the next assistant message."""

    def __init__(self, *steps):
        self.steps, self.bodies = list(steps), []

    def __call__(self, body):
        self.bodies.append(body)
        return {"X-LiteLLM-Model-Group": body["model"]}, self.steps.pop(0)


def call(name, **args):
    return {"content": "", "tool_calls": [{"id": f"c-{name}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}]}


class World:
    """The board and the gate behind owui_tools, for one test."""

    def __init__(self):
        self.log, self.runs, self.executed = [], [], []
        self.wps = {944: {"id": 944, "subject": "Card UI", "lockVersion": 1, "_links": {
            "type": {"title": "Epic"}, "status": {"title": "In Progress"},
            "project": {"title": "civis", "href": "/api/v3/projects/civis"}}}}

    def run_task(self, i):
        self.runs.append(i)
        return {"run_id": "r1", "status": "awaiting-approval", "lane": "dev", "task_id": i,
                "proposal": {"action": "dispatch_build", "target": "civis"}}

    def tools(self, info):
        return owui_tools.build(owui_tools.Actions(
            board=info.get("board") or "", op=lambda b: FakeBoardOP(b, self.log, self.wps),
            capture=lambda g: {"error": "not here"}, run_task=self.run_task,
            run_next=lambda b: {"error": "not here"}))


def tool_deps(router, world, complete_tools=None, run_tools=None, gate=None, log=None):
    d = deps(FakeLiteLLM(), router, rows=BOARD_ROWS, log=log)
    return dataclasses.replace(d, tools=world.tools, complete_tools=complete_tools,
                               run_tools=run_tools, gate=gate)


def no_sdk(*a, **k):
    raise AssertionError("a local-only chat reached the Agent SDK")


def test_u2_u3_make_a_bug_then_build_it_stops_at_the_gate():
    """U2: the bug is created under epic #944 and the reply links it. U3: "build that fix" starts
    a run that waits at the gate; nothing runs."""
    w = World()
    llm = Scripted(call("pm_create", type="Bug", subject="Card-flip flicker", parent="#944"),
                   {"content": "<think>ok</think>Created #901: http://localhost:8097/work_packages/901"})
    d = tool_deps({"model": "private", "reason": "board", "needs_tools": True}, w, llm)
    events, text = run(req("p-civis-poc", "make a bug for the card-flip flicker under epic #944"), d)
    assert w.log[0][:5] == ("create", "civis", "Bug", "Card-flip flicker", 944)
    assert "work_packages/901" in text and "<think>" not in text
    assert any(e["type"] == "status" and e["text"] == "🔧 pm_create" for e in events)
    assert "tools: pm_create" in text
    assert llm.bodies[0]["tools"] and llm.bodies[0]["model"] == "private"
    tool_msg = llm.bodies[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"])["created"]["id"] == 901

    llm = Scripted(call("build_task", id="901"),
                   {"content": "Run r1 waits at the gate. Say approve."})
    d = tool_deps({"model": "fast", "reason": "build"}, w, llm)          # needs_tools missed:
    events, text = run(req("p-civis-poc", "make a bug…", "Created #901", "build that fix"), d)
    assert w.runs == [901] and "waits at the gate" in text                # the hint catches it
    assert w.executed == []


def test_a_local_only_chat_never_reaches_the_sdk():
    w = World()
    llm = Scripted(call("pm_comment", id="944", text="hi"), {"content": "done"})
    d = tool_deps({"model": "private", "reason": "r", "needs_tools": True}, w, llm,
                  run_tools=no_sdk)
    _, text = run(req("p-notes", "comment hi on #944"), d)
    assert text.startswith("done") and w.log[0][:2] == ("comment", "notes")
    for body in llm.bodies:
        assert body["model"] in R.LOCAL and "disable_fallbacks" in body or all(
            m in R.LOCAL for m in body.get("fallbacks", []))


def test_a_cloud_chat_uses_the_sdk_with_the_chats_tools():
    w = World()
    seen = {}

    def sdk(system, user, tools, alias):
        seen.update(system=system, user=user, alias=alias, names=set(tools))
        tools["pm_comment"]["call"]({"id": "944", "text": "from the SDK"})
        return {"reply": "Commented on #944.", "tool_calls": [{"name": "pm_comment"}],
                "stopped": None}

    d = tool_deps({"model": "codex", "reason": "r", "needs_tools": True}, w, run_tools=sdk)
    _, text = run(req("p-civis-poc", "earlier", "ok", "comment on #944"), d)
    assert seen["alias"] == "balanced" and "Earlier in this chat" in seen["user"]
    assert seen["names"] == set(owui_tools.READS + owui_tools.GREEN + owui_tools.GATED)
    assert seen["system"].startswith(O.TOOLS_SYSTEM)
    assert w.log[0] == ("comment", "civis", 944, "from the SDK")
    assert "routed: balanced" in text and "served by: balanced" in text
    assert "codex can't call tools here" in text and "fallback" not in text


def test_when_the_sdk_cannot_run_the_tools_run_locally():
    w = World()

    def down(*a):
        raise RuntimeError("throttled")

    llm = Scripted({"content": "Nothing pending on #944."})
    d = tool_deps({"model": "deep", "reason": "r", "needs_tools": True}, w, llm, run_tools=down)
    _, text = run(req("p-civis-poc", "what's on #944"), d)
    assert llm.bodies[0]["model"] == "private" and "Agent SDK unavailable" in text
    assert "routed: private" in text and "fallback" not in text


def test_a_local_tool_turn_fits_the_window():
    """Live 2026-10-12: a project chat's context sized for balanced plus the tool schemas
    overflowed private's 8k. The context is now budgeted for the tool model, and each step is
    cut to fit: oldest history first, then the system prompt."""
    w = World()
    tools = w.tools({"board": "civis"})
    assert O.tool_context_budget("private", tools) < R.context_budget("private")
    room = O._tool_room("private", tools)
    big = "x" * (room * 8)
    msgs = [{"role": "system", "content": big}, {"role": "user", "content": "old " * 900},
            {"role": "assistant", "content": "older reply"}, {"role": "user", "content": "now"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "result"}]
    out = O._fit(msgs, room)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[1]["content"] == "now" and out[0]["content"].endswith("cut to fit the model)")
    assert sum(R.tokens(m["content"]) + 8 for m in out) <= room + 120
    assert O._fit(msgs[3:4], room) == msgs[3:4]


def test_tool_results_are_cut_so_the_prompt_never_overflows():
    """Live 2026-10-15: five tool results of 6000 chars passed 8k, Ollama dropped an assistant
    tool_calls message and 500'd ("A tool message must follow an assistant or tool message")."""
    w = World()
    room = O._tool_room("private", w.tools({"board": "civis"}))
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "now"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]}]
    msgs += [{"role": "tool", "tool_call_id": "a", "content": "r" * 6000} for _ in range(5)]
    out = O._fit(msgs, room)
    assert [m["role"] for m in out] == [m["role"] for m in msgs]
    assert sum(R.tokens(m["content"]) + 8 for m in out) <= room + 120

    llm = Scripted({"content": "ok"})
    d = tool_deps({"model": "private", "reason": "r", "needs_tools": True}, w, llm)
    run(req("p-civis-poc", *(["long " * 2000, "reply " * 2000] * 4), "comment on #944"), d)
    sent = llm.bodies[0]["messages"]
    assert sum(R.tokens(str(m["content"])) + 8 for m in sent) <= room + 120


def test_a_tool_turn_stops_after_its_step_limit():
    w = World()
    llm = Scripted(*[call("pm_get", id="944")] * O.TOOL_STEPS)
    d = tool_deps({"model": "code", "reason": "r", "needs_tools": True}, w, llm)
    _, text = run(req("p-civis-poc", "look at #944"), d)
    assert len(llm.bodies) == O.TOOL_STEPS and f"after {O.TOOL_STEPS} tool steps" in text


def test_a_question_gets_no_tools():
    w = World()
    d = tool_deps({"model": "fast", "reason": "r"}, w, complete_tools=no_sdk, run_tools=no_sdk)
    _, text = run(req("p-civis-poc", "how are you"), d)
    assert text.startswith("the answer")


def test_approve_in_chat_goes_to_the_gate_handler_not_a_model():
    w, decisions, log = World(), [], []

    def gate(decision, do_all):
        decisions.append((decision, do_all))
        return "This is what you would approve: r1. Say approve again."

    d = tool_deps(None, w, complete_tools=no_sdk, run_tools=no_sdk, gate=gate, log=log)
    events, text = run(req("p-civis-poc", "approve"), d)
    assert decisions == [("approved", False)] and "Say approve again" in text
    assert events[-1]["routed"] == "gate" and log[0]["routed"] == "gate"
    run(req("p-civis-poc", "reject all"), d)
    assert decisions[-1] == ("rejected", True)
    assert d.stream.bodies == []                                        # no model was asked


# ── the route ──
fastapi = pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def test_route_streams_json_lines_behind_the_token(monkeypatch):
    monkeypatch.setattr(server, "AUTH_MODE", "off")
    monkeypatch.setattr(server, "_owui_deps", lambda: deps(FakeLiteLLM(), {"model": "fast", "reason": "r"}))
    client = TestClient(server.app)
    monkeypatch.delenv("NOMAD_OWUI_TOKEN", raising=False)
    monkeypatch.setattr(server.owui, "TOKEN_FILES", ())
    assert client.post("/owui/chat", json=req("general", "hi")).status_code == 503
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", "t")
    assert client.post("/owui/chat", json=req("general", "hi")).status_code == 401
    r = client.post("/owui/chat", json=req("general", "hi"), headers={"X-Nomad-Token": "t"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.splitlines()]
    assert events[0]["type"] == "status" and events[-1]["served"] == "fast"


def test_context_route_is_behind_the_token(monkeypatch):
    monkeypatch.setattr(server, "AUTH_MODE", "off")
    monkeypatch.setattr(server, "_owui_deps", lambda: deps(None, bindings=Bindings()))
    monkeypatch.setattr(server.owui, "TOKEN_FILES", ())
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", "t")
    client = TestClient(server.app)
    body = {"chat_id": "c1", "entry": "p-civis-poc", "model": "deep"}
    assert client.post("/owui/context", json=body).status_code == 401
    r = client.post("/owui/context", json=body, headers={"X-Nomad-Token": "t"})
    assert r.status_code == 200 and "# Civis rules" in r.json()["system"]


class FakeNoco:
    def __init__(self):
        self.rows, self.tables = [], set()

    def create_table(self, name, cols):
        self.tables.add(name)

    def find(self, table, field, value):
        return next((r for r in self.rows if r[field] == value), None)

    def create(self, table, fields):
        self.rows.append({"Id": len(self.rows) + 1, **fields})
        return {"Id": len(self.rows)}

    def update(self, table, rid, fields):
        self.rows[rid - 1].update(fields)


def test_bindings_are_stored_in_nocodb_and_cached(monkeypatch):
    noco = FakeNoco()
    monkeypatch.setattr(server, "db", noco)
    monkeypatch.setattr(server, "_owui_chats", {"ok": False, "cache": {}})
    assert server._owui_bound("c1") is None
    server._owui_bind("c1", "new-project")
    assert server._owui_bound("c1") == "new-project" and noco.tables == {"owui_chats"}
    server._owui_bind("c1", "p-civis-poc")              # M3's rename updates the row
    assert [(r["chat_id"], r["entry"]) for r in noco.rows] == [("c1", "p-civis-poc")]
    server._owui_chats["cache"].clear()
    assert server._owui_bound("c1") == "p-civis-poc"


def test_new_project_state_is_stored_in_nocodb(monkeypatch):
    """#1151: one row per chat, the state as JSON, so a restarted engine picks the chat up."""
    noco = FakeNoco()
    monkeypatch.setattr(server, "db", noco)
    monkeypatch.setattr(server, "_owui_np", {"ok": False})
    assert server._owui_np_load("c1") is None
    server._owui_np_save("c1", {"stage": "interview", "q": 2, "id": ""})
    server._owui_np_save("c1", {"stage": "prd", "q": 8, "id": "tide-log", "prd": "# PRD"})
    assert noco.tables == {"owui_new_projects"} and len(noco.rows) == 1
    assert noco.rows[0]["stage"] == "prd" and noco.rows[0]["project"] == "tide-log"
    assert server._owui_np_load("c1")["prd"] == "# PRD"
    d = server._owui_deps()
    assert d.np_load is server._owui_np_load and d.draft is not None


def test_the_engine_wires_the_tools_and_the_gate(monkeypatch):
    """#1147: the route's deps carry the chat's owui_tools, LiteLLM tool calls, the Agent SDK and
    the cockpit's gate handler; the tools act on the chat's board."""
    d = server._owui_deps()
    assert d.gate is server._handle_gate_decision and d.complete_tools is not None
    boards = []
    monkeypatch.setattr("pm_openproject.OpenProject.__init__",
                        lambda self, project=None, **k: boards.append(project))
    tools = d.tools({"board": "civis"})
    assert {n for n, t in tools.items() if t["tier"] == "gated"} == set(owui_tools.GATED)
    tools["pm_comment"]["call"]({"id": "1", "text": "x"})          # fails offline, as data
    assert boards == ["civis"]


# ── truthful action reporting (#1198) ──
FAKE = ("Deleting now.\n✓ 1/5 Local repo cleaned up\n✓ 2/5 Board removed\n"
        "⚠ 5/5 Zulip stream #test deleted\n**Test** is gone\n*routed: medium*\n")


def test_1198_a_fabricated_step_list_and_footer_are_removed_and_the_real_footer_stays():
    llm = FakeLiteLLM(answer=FAKE)
    _, text = run(req("p-civis-poc", "tidy up #1183"), deps(llm, {"model": "fast", "reason": "simple"}))
    assert "✓" not in text and "1/5" not in text and "routed: medium" not in text
    assert O.NOT_RUN in text and "Deleting now." in text
    assert text.endswith("routed: fast (why: simple) · served by: fast")


def test_1198_the_guard_works_when_chunks_split_lines():
    chunks = ["Ok\n✓ 1/", "2 done\n✓ 2/2 ", "done\nfine\nserved by: gpt", " tail"]
    g, out = O.LineGuard(), ""
    for c in chunks:
        out += g.feed(c)
    out += g.flush()
    assert out == f"Ok\n{O.NOT_RUN}\nfine\n{O.NOT_RUN}"


def test_1198_step_lines_pass_when_a_tool_ran_but_footers_never_do():
    t = "✓ 1/2 made\nrouted: x · served by: y"
    assert O.scrub(t, called=True) == f"✓ 1/2 made\n{O.NOT_RUN}"
    assert "1/2" not in O.scrub(t)


def test_1198_the_prompts_forbid_claims_and_delete_counts_as_an_action():
    for p in (O.SYSTEM, O.TOOLS_SYSTEM):
        assert "routed:" in p and "✓ 1/5" in p
    assert O.ACTION_HINT.search("delete the test project")
    llm = FakeLiteLLM()
    run(req("p-civis-poc", "push the branch"), deps(llm, {"model": "fast", "reason": "x"}))
    assert "nothing was done" in llm.bodies[0]["messages"][0]["content"]


def test_1198_a_tool_turn_with_no_calls_is_scrubbed_too():
    w = World()
    llm = Scripted({"content": "Deleted!\n✓ 1/1 Project removed\nrouted: medium"})
    d = tool_deps({"model": "private", "reason": "x", "needs_tools": True}, w, llm)
    _, text = run(req("p-civis-poc", "remove the old draft from the README"), d)
    assert "✓" not in text and "Deleted!" in text and O.NOT_RUN in text


@pytest.mark.parametrize("text", ["delete #1183", "Please delete the test project",
                                  "remove project test", "get rid of this project"])
def test_1198_a_delete_in_other_words_gets_the_command_and_no_model(text):
    llm = FakeLiteLLM()
    ev, out = run(req("p-civis-poc", text), deps(llm, {"model": "fast", "reason": "x"}))
    assert out.startswith("Nothing was deleted.") and "`delete project <id>`" in out
    assert llm.bodies == [] and ev[-1]["routed"] == "delete-help"


def test_1198_a_delete_that_is_not_about_a_project_still_goes_to_the_model():
    llm = FakeLiteLLM()
    run(req("p-civis-poc", "delete the trailing spaces in this snippet"), deps(llm, {"model": "fast", "reason": "x"}))
    assert llm.bodies


# --- #1436 NOMAD Local: the request makes a chat local-only, and stays on the loaded model -----

def test_nomad_local_makes_general_local_only():
    llm = FakeLiteLLM()
    _, text = run({**req("general", "weigh this"), "local_only": True},
                  deps(llm, {"model": "deep", "reason": "hard"}))
    assert llm.bodies[0]["model"] == "private" and llm.bodies[0]["fallbacks"] == ["fast"]
    assert not set(llm.tried) & set(R.CLOUD + R.PAID)


def test_nomad_local_router_may_pick_the_light_model():
    llm = FakeLiteLLM()
    run({**req("p-civis-poc", "hi, what's up?"), "local_only": True},
        deps(llm, {"model": "fast", "reason": "small talk"}))
    assert llm.bodies[0]["model"] == "fast" and llm.bodies[0]["fallbacks"] == ["private"]


def test_local_only_false_never_loosens_a_local_only_project():
    llm = FakeLiteLLM()
    run({**req("p-notes", "weigh this"), "local_only": False},
        deps(llm, {"model": "deep", "reason": "hard"}))
    assert llm.bodies[0]["model"] in R.LOCAL


def test_nomad_local_stays_on_the_loaded_model_without_a_router_call():
    llm = FakeLiteLLM()
    _, text = run({**req("general", "hi"), "local_only": True, "local_loaded": "code"},
                  deps(llm, {"model": "fast", "reason": "small talk"}))
    assert llm.bodies[0]["model"] == "code" and "code-ollama" in llm.bodies[0]["fallbacks"]
    assert "code is already loaded" in text


def test_loaded_is_ignored_outside_a_local_only_chat():
    llm = FakeLiteLLM()
    run({**req("general", "weigh this"), "local_loaded": "code"},
        deps(llm, {"model": "deep", "reason": "hard"}))
    assert llm.bodies[0]["model"] == "deep"


def test_an_override_beats_the_loaded_model():
    llm = FakeLiteLLM()
    run({**req("general", "!fast hi"), "local_only": True, "local_loaded": "code"}, deps(llm, None))
    assert llm.bodies[0]["model"] == "fast"
