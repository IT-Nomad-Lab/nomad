"""NOMAD v2 · 2D — pluggable specialist runtime.

Runs a specialist's reasoning either NATIVELY on the **Claude Agent SDK** (default) or via the
**LiteLLM** stand-in (fallback / non-Claude aliases). ADR-001's adapter-first trajectory lands
here: specialists become SDK agents (Skill = system prompt; MCP tools attach at the SDK layer).

- SDK path uses the local Claude login (same Opus/Sonnet the bridge serves) — no API key.
- Falls back to LiteLLM automatically if the SDK is unavailable, errors, or is rate-limited, so
  the system never breaks. Force a path with NOMAD_RUNTIME=sdk|litellm.
"""
import os

import llm

# Claude alias → Agent-SDK model shortcut. Non-Claude aliases use the LiteLLM path.
_SDK_MODEL = {"deep": "opus", "balanced": "sonnet"}


def _sdk_available():
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except Exception:
        return False


USE_SDK = os.environ.get("NOMAD_RUNTIME", "sdk" if _sdk_available() else "litellm").lower() == "sdk"


def _run_sdk(system: str, user: str, alias: str, timeout_s: float | None = None) -> str:
    import anyio
    from claude_agent_sdk import query, ClaudeAgentOptions
    parts = []

    async def go():
        kw = dict(system_prompt=system, max_turns=1, allowed_tools=[])
        if alias in _SDK_MODEL:
            kw["model"] = _SDK_MODEL[alias]
        with anyio.fail_after(timeout_s):         # None: no limit
            async for msg in query(prompt=user, options=ClaudeAgentOptions(**kw)):
                for blk in getattr(msg, "content", []) or []:
                    t = getattr(blk, "text", None)
                    if t:
                        parts.append(t)

    anyio.run(go)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("SDK returned no text")
    return text


def run(system: str, user: str, alias: str = "balanced", max_tokens: int = 600,
        timeout_s: float | None = None) -> str:
    """Run one reasoning turn. SDK-native by default; falls back to LiteLLM on any issue.
    `timeout_s` bounds each path; without it the SDK has no limit and LiteLLM has 120 s."""
    if USE_SDK and alias in _SDK_MODEL:
        try:
            return _run_sdk(system, user, alias, timeout_s)
        except Exception:
            pass                      # rate-limited / unavailable → LiteLLM stand-in
    if timeout_s:
        return llm.chat(alias, system, user, max_tokens, timeout=timeout_s)
    return llm.chat(alias, system, user, max_tokens)


# ── #970: a chat turn that can call tools (PRD docs/prd/cockpit-chat-tools.md 6) ─────────────
#
# The bounds on one tool turn. They are the turn's cost cap as well as its latency cap: the chat runs
# on the local Claude login, which bills no tokens per call, so what a turn costs is how many model
# turns it takes and how much tool output it reads. Starting values from the PRD; tune them from
# real turns.
TOOL_MAX_TURNS = int(os.environ.get("NOMAD_CHAT_MAX_TURNS") or 6)
TOOL_TIMEOUT_S = float(os.environ.get("NOMAD_CHAT_TIMEOUT_S") or 60)
TOOL_RESULT_MAX = int(os.environ.get("NOMAD_CHAT_TOOL_RESULT_MAX") or 8000)
_DISCONNECT_GRACE_S = 10          # closing the CLI after a cut; past it the turn is abandoned
TOOL_SERVER = "nomad-chat"        # tool names reach the model as mcp__nomad-chat__<name>

# Named so the options say it twice: `tools=[]` turns the built-ins off, and these are refused by
# name as well, in case an SDK version ignores `tools`. Neither list is what keeps them out of reach;
# the allowlist is. This is the second lock.
BUILTIN_TOOLS = ("Bash", "BashOutput", "KillShell", "KillBash", "Write", "Edit", "MultiEdit",
                 "Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "Task", "Agent",
                 "NotebookEdit", "NotebookRead", "TodoWrite", "ExitPlanMode", "SlashCommand",
                 "Skill", "ListMcpResourcesTool", "ReadMcpResourceTool")


class ToolsUnavailable(RuntimeError):
    """The tool path couldn't run: no SDK, a non-Claude alias, or the SDK failed to start."""


def tool_name(name: str) -> str:
    return f"mcp__{TOOL_SERVER}__{name}"


def cap_tool_result(text: str, limit: int | None = None) -> str:
    """R3: one tool result is at most `limit` characters, and says so when it was cut."""
    limit = limit or TOOL_RESULT_MAX
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[cut: the result had {len(text)} characters, shown {limit}]"


def run_tools(system: str, user: str, tools: dict, alias: str = "balanced",
              max_turns: int | None = None, timeout_s: float | None = None) -> dict:
    """One chat turn on the Agent SDK with `tools` as the only tools the model can call.

    `tools` maps a name to `{"description", "input_schema", "call"}`, where `call(args)` returns a
    JSON-able dict and never raises. Nothing else is reachable: built-in tools are off, every tool
    not named in `tools` is refused, and the user's own MCP servers and settings aren't loaded.

    Returns `{"reply", "tool_calls", "stopped", "turns", "cost_usd"}`. `stopped` is None, "turns"
    when `max_turns` ran out, or "time" when `timeout_s` did; `reply` is then whatever the model had
    said by that point, possibly "". Raises ToolsUnavailable when the SDK can't run the turn at all,
    so the caller can fall back (R5)."""
    if not (USE_SDK and alias in _SDK_MODEL):
        raise ToolsUnavailable("the Agent SDK is not this engine's runtime")
    try:
        return _run_sdk_tools(system, user, tools, alias, max_turns or TOOL_MAX_TURNS,
                              timeout_s or TOOL_TIMEOUT_S)
    except ToolsUnavailable:
        raise
    except Exception as e:
        raise ToolsUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from e


def sdk_tool_options(system: str, tools: dict, alias: str, max_turns: int, sdk_tools: list,
                     can_use_tool=None) -> dict:
    """The ClaudeAgentOptions keywords for a tool turn. Separate so a test can read exactly what the
    SDK is handed (#971's allowlist test)."""
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server
    import dataclasses
    fields = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
    kw = dict(system_prompt=system, model=_SDK_MODEL[alias], max_turns=max_turns,
              mcp_servers={TOOL_SERVER: create_sdk_mcp_server(name=TOOL_SERVER, version="1.0.0",
                                                              tools=sdk_tools)},
              allowed_tools=[tool_name(n) for n in tools],
              disallowed_tools=list(BUILTIN_TOOLS))
    optional = {"tools": [],                          # built-ins off
                "setting_sources": [],                # no user or project settings, no their MCP
                "can_use_tool": can_use_tool,         # anything that still asks: refused
                "extra_args": {"strict-mcp-config": None}}  # only the MCP server above
    kw.update({k: v for k, v in optional.items() if k in fields and v is not None})
    return kw


def _run_sdk_tools(system, user, tools, alias, max_turns, timeout_s) -> dict:
    import json
    import logging
    import anyio
    from anyio import to_thread
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, tool
    log = logging.getLogger("nomad.chat")
    out = {"reply": "", "tool_calls": [], "stopped": None, "turns": 0, "cost_usd": None}
    texts = []

    def _sdk_tool(name, spec):
        async def handler(args):
            args = dict(args or {})
            out["tool_calls"].append({"name": name, "args": args})
            log.info("chat tool call: %s %s", name, json.dumps(args, default=str)[:300])
            # a read stuck past the time limit is left behind, not waited for
            try:
                res = await to_thread.run_sync(spec["call"], args, abandon_on_cancel=True)
            except TypeError:                                   # anyio < 4.1
                res = await to_thread.run_sync(spec["call"], args, cancellable=True)
            text = cap_tool_result(json.dumps(res, default=str, ensure_ascii=False))
            return {"content": [{"type": "text", "text": text}]}
        return tool(name, spec["description"], spec["input_schema"])(handler)

    allowed = {tool_name(n) for n in tools}

    async def refuse_the_rest(name, _input, _ctx):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        if name in allowed:
            return PermissionResultAllow()
        log.warning("chat tool refused: %s", name)
        return PermissionResultDeny(message=f"{name} is not available in the chat")

    options = ClaudeAgentOptions(**sdk_tool_options(
        system, tools, alias, max_turns, [_sdk_tool(n, s) for n, s in tools.items()],
        refuse_the_rest))

    async def go():
        with anyio.move_on_after(timeout_s + _DISCONNECT_GRACE_S) as outer:
            async with ClaudeSDKClient(options=options) as client:
                with anyio.move_on_after(timeout_s) as inner:
                    await client.query(user)
                    async for msg in client.receive_response():
                        kind = type(msg).__name__
                        if kind == "AssistantMessage":
                            said = "".join(getattr(b, "text", "") or ""
                                           for b in getattr(msg, "content", []) or []).strip()
                            if said:
                                texts.append(said)
                        elif kind == "ResultMessage":
                            out["turns"] = getattr(msg, "num_turns", 0) or 0
                            out["cost_usd"] = getattr(msg, "total_cost_usd", None)
                            if getattr(msg, "subtype", "") == "error_max_turns":
                                out["stopped"] = "turns"
                if inner.cancelled_caught:
                    out["stopped"] = "time"
        if outer.cancelled_caught:
            out["stopped"] = "time"

    anyio.run(go)
    out["reply"] = texts[-1] if texts else ""
    log.info("chat turn: %d tool call(s), %s turn(s), stopped=%s, cost_usd=%s",
             len(out["tool_calls"]), out["turns"], out["stopped"], out["cost_usd"])
    return out


def active() -> str:
    return "claude-agent-sdk" if USE_SDK else "litellm"
