"""#1132 · the Claude and Codex bridges answer stream=true with SSE.

Without it LiteLLM read their JSON body as an empty stream, so every streamed deep, balanced or
codex reply was empty, and counted as a success, so no fallback ran.

Run: uv run --no-project --with pytest python -m pytest dispatcher/test_bridge_sse.py -q
"""
import importlib.util
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=["claude", "codex"])
def bridge(request, monkeypatch):
    if request.param == "claude":
        mod = _load("claude_bridge", "claude-bridge/bridge.py")
        monkeypatch.setattr(mod, "call_claude", lambda model, system, prompt: (
            "the whole answer", {"input_tokens": 3, "output_tokens": 4}))
    else:
        mod = _load("codex_bridge", "codex-bridge/bridge.py")
        monkeypatch.setattr(mod, "call_codex", lambda prompt: ("the whole answer", (3, 4)))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
    srv.shutdown()


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.headers.get("Content-Type"), r.read().decode()


MSGS = [{"role": "user", "content": "hi"}]


def test_a_plain_request_still_gets_json(bridge):
    ctype, body = post(bridge, {"model": "sonnet", "messages": MSGS})
    assert ctype == "application/json"
    assert json.loads(body)["choices"][0]["message"]["content"] == "the whole answer"


def test_a_streamed_request_gets_sse_the_way_litellm_reads_it(bridge):
    ctype, body = post(bridge, {"model": "sonnet", "messages": MSGS, "stream": True})
    assert ctype == "text/event-stream"
    events = [line[6:] for line in body.split("\n\n") if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == "the whole answer"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["completion_tokens"] == 4
