"""#970 · chat_reply's tool turn on the Agent SDK (PRD docs/prd/cockpit-chat-tools.md 4 and 6).

The SDK isn't installed in the engine image, so `claude_agent_sdk` here is a fake with the same
names. It records the options it is handed and plays a scripted conversation, calling the real tool
handlers runtime.py registers. What is checked is what NOMAD controls: the options, the allowlist,
the bounds and the reply the operator sees.

Run: python3 -m pytest v2/test_chat_reply_tools.py -q   (engine deps live in the container)
"""
import dataclasses
import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import anyio  # noqa: E402
import server  # noqa: E402
import runtime  # noqa: E402
import chat_tools  # noqa: E402
import chat_pm_tools  # noqa: E402

WRITE_NAMES = ("create", "create_plan", "transition", "comment", "update_fields", "relate",
               "unrelate", "watch", "resume", "retry", "capture", "approve", "dispatch")


# ── the fake SDK ────────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class ClaudeAgentOptions:
    system_prompt: str = None
    model: str = None
    max_turns: int = None
    mcp_servers: dict = dataclasses.field(default_factory=dict)
    allowed_tools: list = dataclasses.field(default_factory=list)
    disallowed_tools: list = dataclasses.field(default_factory=list)
    tools: list = None
    setting_sources: list = None
    can_use_tool: object = None
    extra_args: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class TextBlock:
    text: str


@dataclasses.dataclass
class AssistantMessage:
    content: list


@dataclasses.dataclass
class ResultMessage:
    subtype: str = "success"
    num_turns: int = 1
    total_cost_usd: float = 0.0


@dataclasses.dataclass
class PermissionResultAllow:
    pass


@dataclasses.dataclass
class PermissionResultDeny:
    message: str = ""


def _tool(name, description, input_schema):
    def wrap(handler):
        return {"name": name, "description": description, "input_schema": input_schema,
                "handler": handler}
    return wrap


def _create_sdk_mcp_server(name, version="1.0.0", tools=None):
    return {"name": name, "version": version, "tools": tools or []}


@pytest.fixture
def sdk(monkeypatch):
    """Install the fake SDK and turn the SDK path on. `sdk["script"]` is an async generator
    function taking the client; it yields the messages the model would send."""
    state = {"options": None, "prompt": None, "results": {}, "script": None}

    class ClaudeSDKClient:
        def __init__(self, options):
            state["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, prompt):
            state["prompt"] = prompt

        async def call(self, name, args=None):
            """What the CLI does when the model uses a tool: run the registered handler."""
            srv = state["options"].mcp_servers[runtime.TOOL_SERVER]
            t = next(t for t in srv["tools"] if t["name"] == name)
            res = await t["handler"](args or {})
            state["results"][name] = res["content"][0]["text"]
            return res

        def receive_response(self):
            return state["script"](self)

    mod = type(sys)("claude_agent_sdk")
    for name, obj in dict(ClaudeAgentOptions=ClaudeAgentOptions, ClaudeSDKClient=ClaudeSDKClient,
                          tool=_tool, create_sdk_mcp_server=_create_sdk_mcp_server,
                          query=None, PermissionResultAllow=PermissionResultAllow,
                          PermissionResultDeny=PermissionResultDeny).items():
        setattr(mod, name, obj)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    monkeypatch.setattr(runtime, "USE_SDK", True)
    return state


@pytest.fixture
def chat(monkeypatch):
    """Memory stubbed and the no-tools fallback recorded, so a test sees which path answered."""
    import memory_chat
    calls = {"fallback": []}
    monkeypatch.setattr(memory_chat, "classify_memory_intent", lambda m: "")
    monkeypatch.setattr(memory_chat, "recall", lambda *a, **k: [])
    monkeypatch.setattr(memory_chat, "remember", lambda *a, **k: None)
    monkeypatch.setattr(server, "_pipeline_context", lambda *a, **k: "SNAPSHOT-OF-12-RUNS")

    def fallback(system, user, alias="balanced", max_tokens=0, timeout_s=None):
        calls["fallback"].append({"system": system, "timeout_s": timeout_s})
        return "from the snapshot"
    monkeypatch.setattr(runtime, "run", fallback)
    return calls


@pytest.fixture
def board(monkeypatch):
    """The tools' reads, answered from known data, each call recorded."""
    seen = []

    def cockpit(name, args=None):
        seen.append(name)
        return {"pipeline_runs": {"runs": [{"run_id": "25693e0adecf", "status": "failed"}]},
                "pipeline_run_detail": {"run_id": "25693e0adecf", "error": "SMTP 550 rejected"},
                }.get(name, {"error": "autopilot supervisor unreachable"})
    monkeypatch.setattr(chat_tools, "call", cockpit)
    monkeypatch.setattr(chat_pm_tools, "call",
                        lambda name, args=None: seen.append(name) or {"result": "#970 In Progress"})
    return seen


def _say(text):
    return AssistantMessage(content=[TextBlock(text)])


# ── the allowlist ───────────────────────────────────────────────────────────────────────────────

def test_the_sdk_is_handed_the_allowlist_and_nothing_else(sdk, chat, board):
    async def script(client):
        yield _say("Nothing is running.")
        yield ResultMessage()
    sdk["script"] = script
    server.chat_reply("anything running?")
    o = sdk["options"]
    assert sorted(o.allowed_tools) == sorted(f"mcp__nomad-chat__{n}"
                                             for n in server.CHAT_TOOL_ALLOWLIST)
    assert not any("*" in n for n in o.allowed_tools)
    assert list(o.mcp_servers) == ["nomad-chat"]
    registered = [t["name"] for t in o.mcp_servers["nomad-chat"]["tools"]]
    assert sorted(registered) == sorted(server.CHAT_TOOL_ALLOWLIST)
    assert o.tools == [] and o.setting_sources == []                 # built-ins and settings off
    assert {"Bash", "Write", "Edit", "Read", "WebFetch", "Task"} <= set(o.disallowed_tools)
    assert "strict-mcp-config" in o.extra_args


def test_the_allowlist_is_the_thirteen_read_only_tools():
    assert len(server.CHAT_TOOL_ALLOWLIST) == 13
    tools = server._chat_tools()
    assert set(tools) == set(server.CHAT_TOOL_ALLOWLIST)
    for name in tools:
        src = chat_tools.TOOLS.get(name) or chat_pm_tools.TOOLS[name]
        assert src["read_only"] is True
        assert not any(w == name or name.endswith("_" + w) for w in WRITE_NAMES)


def test_a_tool_offered_by_a_module_but_not_named_is_not_reachable(monkeypatch):
    """The list is of what is allowed: a tool added to chat_tools next month stays out of reach."""
    monkeypatch.setitem(chat_tools.TOOLS, "pipeline_retry",
                        {"name": "pipeline_retry", "read_only": True, "handler": lambda: {},
                         "description": "x", "input_schema": chat_tools._NONE})
    assert "pipeline_retry" not in server._chat_tools()


def test_a_named_tool_that_is_not_read_only_is_left_out(monkeypatch):
    entry = dict(chat_tools.TOOLS["pipeline_runs"], read_only=False)
    monkeypatch.setitem(chat_tools.TOOLS, "pipeline_runs", entry)
    assert "pipeline_runs" not in server._chat_tools()


def test_anything_that_asks_for_permission_is_refused(sdk, chat, board):
    async def script(client):
        gate = sdk["options"].can_use_tool
        sdk["bash"] = await gate("Bash", {"command": "rm -rf /"}, None)
        sdk["runs"] = await gate("mcp__nomad-chat__pipeline_runs", {}, None)
        sdk["write"] = await gate("mcp__nomad-pm__transition", {}, None)
        yield _say("ok")
        yield ResultMessage()
    sdk["script"] = script
    server.chat_reply("anything?")
    assert isinstance(sdk["bash"], PermissionResultDeny)
    assert isinstance(sdk["write"], PermissionResultDeny)
    assert isinstance(sdk["runs"], PermissionResultAllow)


# ── several tool calls, one reply ───────────────────────────────────────────────────────────────

def test_max_turns_is_explicit_and_above_one(sdk, chat, board):
    async def script(client):
        yield _say("ok")
        yield ResultMessage()
    sdk["script"] = script
    server.chat_reply("anything?")
    assert sdk["options"].max_turns == runtime.TOOL_MAX_TURNS > 1


def test_a_question_that_needs_live_data_is_answered_from_tool_results(sdk, chat, board, caplog):
    """Q: why did the last run fail? Two lookups, one reply, and the log shows both calls."""
    async def script(client):
        yield _say("Let me look.")
        await client.call("pipeline_runs", {"status": "failed"})
        yield _say("Checking the run.")
        await client.call("pipeline_run_detail", {"run_id": "25693e0adecf"})
        assert "SMTP 550" in sdk["results"]["pipeline_run_detail"]
        yield _say("Run 25693e failed: SMTP 550 rejected.")
        yield ResultMessage(num_turns=3)
    sdk["script"] = script
    with caplog.at_level(logging.INFO, logger="nomad.chat"):
        out = server.chat_reply("why did the last run fail?")
    assert out["reply"] == "Run 25693e failed: SMTP 550 rejected."
    assert out["tools"] == ["pipeline_runs", "pipeline_run_detail"]
    assert board == ["pipeline_runs", "pipeline_run_detail"]
    assert "chat tool call: pipeline_runs" in caplog.text
    assert "chat tool call: pipeline_run_detail" in caplog.text
    assert chat["fallback"] == []


def test_the_tool_turn_reads_live_state_not_the_pasted_snapshot(sdk, chat, board):
    """R4: the model calls pipeline_runs instead of reading a 12-run snapshot in its prompt."""
    async def script(client):
        yield _say("ok")
        yield ResultMessage()
    sdk["script"] = script
    server.chat_reply("anything?")
    assert "SNAPSHOT-OF-12-RUNS" not in sdk["options"].system_prompt


def test_a_question_that_needs_no_tools_is_answered_without_any(sdk, chat, board):
    async def script(client):
        yield _say("The comms lane drafts and sends messages.")
        yield ResultMessage()
    sdk["script"] = script
    out = server.chat_reply("what does the comms lane do?")
    assert out["reply"] == "The comms lane drafts and sends messages."
    assert out["tools"] == [] and board == [] and chat["fallback"] == []


def test_a_pm_tool_is_reachable_too(sdk, chat, board):
    async def script(client):
        await client.call("pm_get", {"id": "970"})
        yield _say("#970 is In Progress.")
        yield ResultMessage(num_turns=2)
    sdk["script"] = script
    out = server.chat_reply("where is #970?")
    assert out["tools"] == ["pm_get"] and "#970 In Progress" in sdk["results"]["pm_get"]


def test_a_tool_error_reaches_the_model_as_text(sdk, chat, board):
    """R6: the error is data the model reads and repeats, never an exception that ends the turn."""
    async def script(client):
        await client.call("autopilot_status")
        yield _say("The autopilot supervisor is unreachable.")
        yield ResultMessage(num_turns=2)
    sdk["script"] = script
    out = server.chat_reply("is the autopilot running?")
    assert "autopilot supervisor unreachable" in sdk["results"]["autopilot_status"]
    assert out["reply"] == "The autopilot supervisor is unreachable."


# ── the bounds ──────────────────────────────────────────────────────────────────────────────────

def test_a_runaway_loop_stops_at_the_turn_cap_and_still_replies(sdk, chat, board):
    async def script(client):
        for _ in range(runtime.TOOL_MAX_TURNS):
            yield _say("Looking again.")
            await client.call("pipeline_runs")
        yield ResultMessage(subtype="error_max_turns", num_turns=runtime.TOOL_MAX_TURNS)
    sdk["script"] = script
    out = server.chat_reply("dig through everything")
    assert out["reply"].startswith("Looking again.")
    assert f"stopped after {runtime.TOOL_MAX_TURNS} steps" in out["reply"]
    assert chat["fallback"] == []


def test_a_turn_cap_with_nothing_said_still_answers(sdk, chat, board):
    async def script(client):
        await client.call("pipeline_runs")
        yield ResultMessage(subtype="error_max_turns", num_turns=6)
    sdk["script"] = script
    out = server.chat_reply("dig")
    assert "stopped after" in out["reply"] and "narrower question" in out["reply"]


def test_a_slow_turn_is_cut_at_the_time_limit_with_a_clear_message(sdk, chat, board, monkeypatch):
    monkeypatch.setattr(runtime, "TOOL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(runtime, "_DISCONNECT_GRACE_S", 0.3)

    async def script(client):
        yield _say("Looking.")
        await anyio.sleep(30)
        yield _say("never")
    sdk["script"] = script
    t0 = time.monotonic()
    out = server.chat_reply("slow one")
    assert time.monotonic() - t0 < 5
    assert "stopped after 0.3 seconds" in out["reply"] and out["reply"].startswith("Looking.")


def test_a_slow_turn_with_nothing_said_still_says_why(sdk, chat, board, monkeypatch):
    monkeypatch.setattr(runtime, "TOOL_TIMEOUT_S", 0.2)

    async def script(client):
        await anyio.sleep(30)
        yield _say("never")
    sdk["script"] = script
    out = server.chat_reply("slow one")
    assert "stopped after 0.2 seconds" in out["reply"] and "before I had an answer" in out["reply"]


def test_a_tool_result_is_capped_and_says_so():
    long = "x" * (runtime.TOOL_RESULT_MAX + 500)
    cut = runtime.cap_tool_result(long)
    assert cut.startswith("x" * runtime.TOOL_RESULT_MAX)
    assert f"had {len(long)} characters" in cut
    assert runtime.cap_tool_result("short") == "short"


def test_a_big_tool_result_reaches_the_model_capped(sdk, chat, board, monkeypatch):
    monkeypatch.setattr(chat_tools, "call", lambda n, a=None: {"runs": ["y" * 20000]})

    async def script(client):
        await client.call("pipeline_runs")
        yield _say("Lots of runs.")
        yield ResultMessage(num_turns=2)
    sdk["script"] = script
    server.chat_reply("list everything")
    assert len(sdk["results"]["pipeline_runs"]) < runtime.TOOL_RESULT_MAX + 100
    assert "[cut:" in sdk["results"]["pipeline_runs"]


def test_the_bounds_are_configured_numbers():
    """The cost bound is the turn cap plus the tool-result cap; the latency bound is the timeout.
    Documented in v2/README.md."""
    assert runtime.TOOL_MAX_TURNS == int(os.environ.get("NOMAD_CHAT_MAX_TURNS") or 6)
    assert runtime.TOOL_TIMEOUT_S == float(os.environ.get("NOMAD_CHAT_TIMEOUT_S") or 60)
    assert runtime.TOOL_RESULT_MAX == int(os.environ.get("NOMAD_CHAT_TOOL_RESULT_MAX") or 8000)
    readme = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")).read()
    for var in ("NOMAD_CHAT_MAX_TURNS", "NOMAD_CHAT_TIMEOUT_S", "NOMAD_CHAT_TOOL_RESULT_MAX"):
        assert var in readme


# ── R5: without the SDK ─────────────────────────────────────────────────────────────────────────

def test_without_the_sdk_the_fallback_answers_and_says_it_could_not_look(chat, monkeypatch):
    monkeypatch.setattr(runtime, "USE_SDK", False)
    out = server.chat_reply("anything running?")
    assert out["reply"] == f"from the snapshot\n\n{server.CHAT_NO_LOOKUP}"
    assert "SNAPSHOT-OF-12-RUNS" in chat["fallback"][0]["system"]
    assert chat["fallback"][0]["timeout_s"] == runtime.TOOL_TIMEOUT_S
    assert out["tools"] == []


def test_an_sdk_that_fails_to_start_falls_back(sdk, chat):
    async def script(client):
        raise RuntimeError("CLI not found")
        yield
    sdk["script"] = script
    out = server.chat_reply("anything running?")
    assert out["reply"].endswith(server.CHAT_NO_LOOKUP) and len(chat["fallback"]) == 1


def test_a_fallback_that_times_out_says_so(chat, monkeypatch):
    monkeypatch.setattr(runtime, "USE_SDK", False)
    monkeypatch.setattr(runtime, "run",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    out = server.chat_reply("anything running?")
    assert "timed out" in out["reply"] and f"{runtime.TOOL_TIMEOUT_S:g} seconds" in out["reply"]
