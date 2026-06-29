#!/usr/bin/env python3
"""NOMAD Claude Code bridge.

Exposes an OpenAI-compatible /v1/chat/completions endpoint that answers using
the LOCAL Claude Code CLI (your subscription/login) instead of the metered
Anthropic API. LiteLLM's `deep` and `balanced` aliases point here.

- Pure stdlib (no pip installs). Runs NATIVELY in WSL (needs the `claude`
  binary + your ~/.claude login), NOT in a container.
- Each request shells out to:  claude -p <prompt> --model <opus|sonnet>
  --system-prompt <sys> --allowedTools "" --setting-sources "" --output-format json
  run in an empty workdir so no project CLAUDE.md / tools leak in.

Caveat: this consumes your Claude Code subscription quota (5-hour windows), not
API credits. Heavy autonomous use can hit those limits — that's expected.
"""
import json
import os
import shutil
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("CLAUDE_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CLAUDE_BRIDGE_PORT", "8088"))
WORKDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_workdir")
CLAUDE = shutil.which("claude") or "/home/linuxbrew/.linuxbrew/bin/claude"
TIMEOUT = int(os.environ.get("CLAUDE_BRIDGE_TIMEOUT", "300"))

BASE_SYSTEM = (
    "You are a helpful AI assistant serving as a model endpoint. Answer the "
    "user's request directly, and follow exactly any output format or protocol "
    "the user's instructions specify (including agent/tool-calling formats such "
    "as 'Action:' / 'Action Input:' blocks). Do not add commentary that breaks a "
    "requested format."
)
# NOTE: Claude Code's own tools are already disabled via --allowedTools "" at the
# call site, so we must NOT tell the model to avoid tools here — that would fight
# CrewAI's text-based tool protocol and make agents narrate instead of act.


def _text(content):
    """OpenAI content may be a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def render(messages):
    system_parts, convo = [], []
    for m in messages:
        role = m.get("role")
        txt = _text(m.get("content"))
        if role == "system":
            system_parts.append(txt)
        elif role in ("user", "assistant", "tool"):
            convo.append((role, txt))
    system = BASE_SYSTEM + (("\n\n" + "\n\n".join(system_parts)) if system_parts else "")

    if len(convo) == 1 and convo[0][0] == "user":
        prompt = convo[0][1]
    else:
        lines = []
        for role, txt in convo:
            label = {"user": "User", "assistant": "Assistant", "tool": "Tool"}[role]
            lines.append(f"{label}: {txt}")
        lines.append("Assistant:")
        prompt = "\n\n".join(lines)
    return system, prompt


def claude_model(name):
    n = (name or "").lower()
    if "opus" in n:
        return "opus"
    if "haiku" in n:
        return "haiku"
    return "sonnet"


def call_claude(model, system, prompt):
    os.makedirs(WORKDIR, exist_ok=True)
    cmd = [
        CLAUDE, "-p", prompt,
        "--model", model,
        "--system-prompt", system,
        "--allowedTools", "",
        "--setting-sources", "",
        "--output-format", "json",
    ]
    proc = subprocess.run(
        cmd, cwd=WORKDIR, capture_output=True, text=True, timeout=TIMEOUT,
        env={**os.environ, "ANTHROPIC_API_KEY": ""},  # force subscription auth, never the API key
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[:500]}")
    data = json.loads(proc.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude error: {data.get('result')}")
    usage = data.get("usage", {}) or {}
    return data.get("result", ""), usage


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quieter logs
        pass

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            now = int(time.time())
            self._send(200, {"object": "list", "data": [
                {"id": "opus", "object": "model", "created": now, "owned_by": "claude-code"},
                {"id": "sonnet", "object": "model", "created": now, "owned_by": "claude-code"},
            ]})
        elif self.path.rstrip("/") in ("/health", ""):
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            model_req = req.get("model", "sonnet")
            system, prompt = render(req.get("messages", []))
            result, usage = call_claude(claude_model(model_req), system, prompt)
        except Exception as e:  # surface as an OpenAI-style error so LiteLLM logs it
            self._send(502, {"error": {"message": str(e), "type": "claude_bridge_error"}})
            return

        pt = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        ct = usage.get("output_tokens", 0)
        self._send(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_req,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                      "total_tokens": pt + ct},
        })


if __name__ == "__main__":
    print(f"NOMAD Claude bridge on http://{HOST}:{PORT}  (claude={CLAUDE})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
