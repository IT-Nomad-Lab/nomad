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


def _run_sdk(system: str, user: str, alias: str) -> str:
    import anyio
    from claude_agent_sdk import query, ClaudeAgentOptions
    parts = []

    async def go():
        kw = dict(system_prompt=system, max_turns=1, allowed_tools=[])
        if alias in _SDK_MODEL:
            kw["model"] = _SDK_MODEL[alias]
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


def run(system: str, user: str, alias: str = "balanced", max_tokens: int = 600) -> str:
    """Run one reasoning turn. SDK-native by default; falls back to LiteLLM on any issue."""
    if USE_SDK and alias in _SDK_MODEL:
        try:
            return _run_sdk(system, user, alias)
        except Exception:
            pass                      # rate-limited / unavailable → LiteLLM stand-in
    return llm.chat(alias, system, user, max_tokens)


def active() -> str:
    return "claude-agent-sdk" if USE_SDK else "litellm"
