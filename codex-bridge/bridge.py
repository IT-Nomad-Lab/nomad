#!/usr/bin/env python3
"""NOMAD Codex bridge.

Exposes an OpenAI-compatible /v1/chat/completions endpoint answered by the LOCAL
OpenAI Codex CLI (your ChatGPT subscription login), not the metered OpenAI API.
LiteLLM's `codex` alias points here, and every other alias falls back to it.

Why it exists: until now every subscription-backed call in NOMAD went through the
one `claude` CLI. When the MAX subscription hits a 5-hour window the only thing
left was llama3.1:8b. Codex is a SECOND subscription on a different vendor, so a
throttle on one no longer degrades the whole stack.

- Pure stdlib (no pip installs). Runs NATIVELY in WSL (needs the `codex` binary
  + your ~/.codex login), NOT in a container.
- Each request shells out to:  codex exec --skip-git-repo-check --ephemeral
  -s read-only -C <empty workdir> --json -o <tmp> -   (prompt on stdin)
  read-only sandbox + an empty workdir so no project file or AGENTS.md leaks in
  and the model cannot write anything. This is a MODEL endpoint, not an agent.

Caveat: this consumes ChatGPT subscription quota, not API credits. Heavy
autonomous use can hit those limits — same trade as the Claude bridge.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler

HOST = os.environ.get("CODEX_BRIDGE_HOST", "127.0.0.1")   # loopback only (#821); LiteLLM reaches it via host.docker.internal
PORT = int(os.environ.get("CODEX_BRIDGE_PORT", "8089"))   # 8088 claude · 8089 codex
WORKDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_workdir")
CODEX = shutil.which("codex") or "/home/linuxbrew/.linuxbrew/bin/codex"
TIMEOUT = int(os.environ.get("CODEX_BRIDGE_TIMEOUT", "300"))
# Empty = let the CLI use whatever model its config defaults to, so a model
# rename upstream doesn't break the bridge. Pin it here only on purpose.
MODEL = os.environ.get("CODEX_BRIDGE_MODEL", "").strip()

BASE_SYSTEM = (
    "You are a helpful AI assistant serving as a model endpoint. Answer the "
    "user's request directly, and follow exactly any output format or protocol "
    "the user's instructions specify (including agent/tool-calling formats such "
    "as 'Action:' / 'Action Input:' blocks). Do not add commentary that breaks a "
    "requested format. Do not attempt to read or modify files; answer from the "
    "conversation alone."
)


def _text(content):
    """OpenAI content may be a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def render(messages):
    """Flatten an OpenAI message list into one prompt. Codex exec has no
    --system-prompt flag, so the system text is folded in at the top."""
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
        body = convo[0][1]
    else:
        lines = []
        for role, txt in convo:
            label = {"user": "User", "assistant": "Assistant", "tool": "Tool"}[role]
            lines.append(f"{label}: {txt}")
        lines.append("Assistant:")
        body = "\n\n".join(lines)
    return f"{system}\n\n---\n\n{body}"


def _usage(jsonl):
    """Best-effort token counts from the --json event stream. The event schema is
    not a stable contract, so this never raises — worst case usage reads as zero."""
    pt = ct = 0
    for line in jsonl.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        u = ev.get("usage") or (ev.get("msg") or {}).get("usage") or {}
        if isinstance(u, dict):
            pt = u.get("input_tokens", u.get("prompt_tokens", pt)) or pt
            ct = u.get("output_tokens", u.get("completion_tokens", ct)) or ct
    return int(pt or 0), int(ct or 0)


def call_codex(prompt):
    os.makedirs(WORKDIR, exist_ok=True)
    fd, out_path = tempfile.mkstemp(prefix="codex-", suffix=".txt")
    os.close(fd)
    cmd = [
        CODEX, "exec",
        "--skip-git-repo-check",     # the empty workdir is not a repo
        "--ephemeral",               # don't litter ~/.codex with session files
        "--ignore-user-config",      # a personal config.toml must not change model behaviour here
        "-s", "read-only",           # the model gets no write access, ever
        "-C", WORKDIR,
        "--color", "never",
        "--json",
        "-o", out_path,
        "-",                         # prompt arrives on stdin
    ]
    if MODEL:
        cmd[2:2] = ["-m", MODEL]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT,
            cwd=WORKDIR,
            # A key in the environment would silently switch billing from the
            # subscription to metered API credits. Blank it, like the Claude bridge.
            env={**os.environ, "OPENAI_API_KEY": ""},
        )
        if proc.returncode != 0:
            raise RuntimeError(f"codex exited {proc.returncode}: {(proc.stderr or '')[:500]}")
        with open(out_path, encoding="utf-8", errors="replace") as fh:
            result = fh.read().strip()
        if not result:
            raise RuntimeError(f"codex returned no message: {(proc.stderr or proc.stdout)[:500]}")
        return result, _usage(proc.stdout)
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply(self, req, completion):
        """The completion as JSON, or as SSE when the caller asked to stream (#1132). The CLI
        gives the whole answer at once, so the stream is one content chunk, a stop chunk with
        usage, and [DONE]. Without this LiteLLM reads the JSON body as an empty stream."""
        if not req.get("stream"):
            self._send(200, completion)
            return
        base = {k: completion[k] for k in ("id", "created", "model")}
        base["object"] = "chat.completion.chunk"
        content = completion["choices"][0]["message"]["content"]
        chunks = [{**base, "choices": [{"index": 0, "finish_reason": None,
                                        "delta": {"role": "assistant", "content": content}}]},
                  {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": completion.get("usage")}]
        body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
        body += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quieter logs
        pass

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/v1/models":
            now = int(time.time())
            self._send(200, {"object": "list", "data": [
                {"id": "codex", "object": "model", "created": now, "owned_by": "codex-cli"},
            ]})
        elif path in ("/health", ""):
            self._send(200, {"status": "ok", "service": "codex-bridge",
                             "codex": CODEX, "model": MODEL or "(cli default)"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            model_req = req.get("model", "codex")
            result, (pt, ct) = call_codex(render(req.get("messages", [])))
        except Exception as e:  # surface as an OpenAI-style error so LiteLLM fails over
            self._send(502, {"error": {"message": str(e), "type": "codex_bridge_error"}})
            return

        self._reply(req, {
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
    import importlib.util   # dispatcher/bridge_bind.py, by path: loopback + NOMAD_BRIDGE_IP (#383)
    spec = importlib.util.spec_from_file_location("bridge_bind", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "dispatcher", "bridge_bind.py"))
    bridge_bind = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_bind)
    print(f"NOMAD Codex bridge on http://{HOST}:{PORT}  (codex={CODEX}, "
          f"model={MODEL or 'cli default'})", flush=True)
    bridge_bind.serve_http(HOST, PORT, Handler)
