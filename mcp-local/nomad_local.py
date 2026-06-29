"""NOMAD local-agent bridge (MCP) — delegate a sub-task to a LOCAL model on your own hardware.

The primitive that lets Claude Code orchestrate local + cloud: while Claude (cloud) conducts, it
hands cheap / private / bulk sub-tasks to a model on your GPU. Talks DIRECTLY to the native Ollama
(reachable from the editor's process), so nothing leaves the machine and there's no container hop.

Tools:
  local_llm(prompt, system, model, max_tokens)  → run a prompt on a local model, return the text
  list_local_models()                           → what Ollama models are installed right now
"""
import os

import requests
from mcp.server.fastmcp import FastMCP

# Friendly role aliases → installed Ollama tags (mirrors the LiteLLM aliases). Pass a full tag
# (anything with a ':') to use it directly; an unknown alias falls back to the first chat model.
ALIASES = {"fast": "llama3.1:8b", "private": "deepseek-r1:32b", "code": "qwen2.5-coder:32b"}

_CANDIDATES = [c for c in (
    os.environ.get("OLLAMA_URL"),
    "http://host.docker.internal:11434",
    "http://172.21.128.1:11434",          # WSL → Windows-host gateway
    "http://127.0.0.1:11434",
) if c]
_base = None


def _ollama():
    """Find a reachable Ollama base URL once, then reuse it."""
    global _base
    if _base:
        return _base
    for b in _CANDIDATES:
        try:
            requests.get(b.rstrip("/") + "/api/tags", timeout=2)
            _base = b.rstrip("/")
            return _base
        except Exception:
            continue
    _base = _CANDIDATES[0].rstrip("/")
    return _base


def _resolve(model: str):
    if ":" in model:                       # already a full tag
        return model
    if model in ALIASES:
        try:
            tags = {m["name"] for m in requests.get(_ollama() + "/api/tags", timeout=5).json().get("models", [])}
            if ALIASES[model] in tags:
                return ALIASES[model]
            return next(iter(t for t in tags if not t.startswith("nomic")), ALIASES[model])
        except Exception:
            return ALIASES[model]
    return model


mcp = FastMCP("nomad-local")


@mcp.tool()
def local_llm(prompt: str, system: str = "", model: str = "fast", max_tokens: int = 800) -> str:
    """Run a prompt on a LOCAL model (this machine's GPU) and return the completion text.

    Use this to offload cheap, bulk, or PRIVATE sub-tasks you don't want to send to the cloud:
    summarizing, classifying, drafting, extracting, quick reasoning over local/sensitive data.
    `model` is a role alias — 'fast' (llama3.1:8b, always-on workhorse), 'private' (deepseek-r1:32b,
    local reasoning), 'code' (qwen2.5-coder:32b) — or a full Ollama tag. Returns the text."""
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    try:
        r = requests.post(_ollama() + "/api/chat", timeout=600, json={
            "model": _resolve(model), "messages": messages, "stream": False,
            "options": {"num_predict": max_tokens}})
        r.raise_for_status()
        return (r.json().get("message", {}) or {}).get("content", "").strip() or "[empty response]"
    except Exception as e:
        return f"[local_llm error: {str(e)[:240]}]"


@mcp.tool()
def list_local_models() -> str:
    """List the local Ollama models installed on this machine (name + size)."""
    try:
        models = requests.get(_ollama() + "/api/tags", timeout=10).json().get("models", [])
        return "\n".join(f"- {m['name']} ({round(m.get('size', 0) / 1e9, 1)} GB)"
                         for m in models) or "(no local models)"
    except Exception as e:
        return f"[ollama error: {str(e)[:200]}]"


if __name__ == "__main__":
    mcp.run()
