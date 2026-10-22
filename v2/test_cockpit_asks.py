"""#1363 · the cockpit's Pending asks view: lists the agent bridge's asks and answers them.

A stub bridge (real HTTP, same contract as agent-bridge/server.py) holds asks and a "run" per ask
that waits on the decision, so the gate test proves an answer from the cockpit continues the run.

Run: python3 -m pytest v2/test_cockpit_asks.py -q   (route tests need fastapi: the engine image)
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cockpit_asks as A  # noqa: E402

TOKEN = "t0k3n-for-tests"
QS = [{"question": "Which db?", "header": "DB", "multiSelect": False,
       "options": [{"label": "sqlite", "description": "file"}, {"label": "postgres", "description": "server"}]}]


def ask(i, state="pending", kind="permission", created="2026-10-16T10:00:0%dZ", **kw):
    a = {"ask_id": f"ask-{i:010x}", "run_id": f"run{i}", "chat_id": f"chat-{i}", "kind": kind,
         "tool": "Bash" if kind == "permission" else "AskUserQuestion", "summary": f"summary {i}",
         "reason": "", "created": created % i, "state": state}
    if kind == "question":
        a["questions"] = QS
    a.update(kw)
    return a


class StubBridge:
    """Records requests; `runs` maps ask_id to an Event the 'run' waits on, set by a decision."""

    def __init__(self, asks):
        self.asks = {a["ask_id"]: a for a in asks}
        self.seen, self.decisions = [], []
        self.runs = {k: threading.Event() for k, a in self.asks.items() if a["state"] == "pending"}
        self.resume = {}
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                me.seen.append(("GET", self.path, self.headers.get("X-Nomad-Token")))
                if self.headers.get("X-Nomad-Token") != TOKEN:
                    return self._json(401, {"error": "token"})
                from urllib.parse import parse_qs, urlparse
                u = urlparse(self.path)
                st = parse_qs(u.query).get("state", [""])[0]
                rows = [a for a in me.asks.values() if not st or a["state"] == st]
                self._json(200, {"pending": rows})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                me.seen.append(("POST", self.path, self.headers.get("X-Nomad-Token")))
                if self.headers.get("X-Nomad-Token") != TOKEN:
                    return self._json(401, {"error": "token"})
                a = me.asks.get(body.get("ask_id"))
                if a is None:
                    return self._json(409, {"ok": False, "error": "no such ask"})
                if a["state"] == "expired":
                    return self._json(409, {"ok": False, "state": "expired", "error": "expired"})
                if a["state"] != "pending":
                    return self._json(409, {"ok": False, "error": "already answered"})
                a["state"] = "answered"
                me.decisions.append(body)
                me.resume[a["ask_id"]] = body
                me.runs[a["ask_id"]].set()                      # the run continues
                self._json(200, {"ok": True, "ask_id": a["ask_id"], "decision": body["decision"]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def bridge(monkeypatch):
    made = []

    def make(asks):
        b = StubBridge(asks)
        made.append(b)
        monkeypatch.setattr(A, "BRIDGE_URL", b.url)
        return b
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", TOKEN)
    yield make
    for b in made:
        b.httpd.shutdown()


# ── listing ─────────────────────────────────────────────────────────────────────────────────────

def test_list_sends_the_token_and_puts_pending_first_newest_first(bridge):
    b = bridge([ask(1, "answered"), ask(2), ask(3, "expired"), ask(4)])
    code, out = A.list_asks()
    assert code == 200
    assert [a["ask_id"][-1] for a in out["asks"]] == ["4", "2", "3", "1"]
    assert b.seen[0] == ("GET", "/agent/pending", TOKEN)


def test_state_filter_goes_to_the_bridge_and_a_bad_state_never_does(bridge):
    b = bridge([ask(1, "answered"), ask(2)])
    code, out = A.list_asks("pending")
    assert code == 200 and len(out["asks"]) == 1
    assert b.seen[-1][1] == "/agent/pending?state=pending"
    n = len(b.seen)
    assert A.list_asks("bogus")[0] == 400 and len(b.seen) == n


def test_settled_states_come_through_for_read_only_rows(bridge):
    bridge([ask(1, "answered"), ask(2, "timeout"), ask(3, "expired"), ask(4, "cancelled")])
    states = {a["state"] for a in A.list_asks()[1]["asks"]}
    assert states == {"answered", "timeout", "expired", "cancelled"}


def test_bridge_down_is_a_503_the_page_can_print(monkeypatch):
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", TOKEN)
    monkeypatch.setattr(A, "BRIDGE_URL", "http://127.0.0.1:9")
    code, out = A.list_asks()
    assert code == 503 and out["error"] == A.DOWN


def test_no_token_is_refused_before_any_request(monkeypatch):
    monkeypatch.delenv("NOMAD_OWUI_TOKEN", raising=False)
    monkeypatch.setattr(A.owui, "TOKEN_FILES", ())
    code, out = A.list_asks()
    assert code == 503 and "token" in out["error"]


def test_a_wrong_token_is_not_passed_as_401_to_the_browser(bridge, monkeypatch):
    bridge([ask(1)])
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", "other")
    assert A.list_asks()[0] == 502


# ── answering ───────────────────────────────────────────────────────────────────────────────────

def test_approve_continues_the_run(bridge):
    """The gate: a cockpit answer reaches the bridge and the waiting run resumes with it."""
    a = ask(1)
    b = bridge([a])
    assert not b.runs[a["ask_id"]].is_set()
    code, out = A.decide({"ask_id": a["ask_id"], "decision": "approve"})
    assert code == 200 and out["ok"]
    assert b.runs[a["ask_id"]].wait(2), "run did not continue"
    assert b.resume[a["ask_id"]] == {"ask_id": a["ask_id"], "decision": "approve"}


def test_question_answers_and_deny_note_are_forwarded(bridge):
    q, p = ask(1, kind="question"), ask(2)
    b = bridge([q, p])
    answers = {"Which db?": "postgres, my own"}
    assert A.decide({"ask_id": q["ask_id"], "decision": "approve", "answers": answers})[0] == 200
    assert A.decide({"ask_id": p["ask_id"], "decision": "deny", "answers": {"_note": "not now"}})[0] == 200
    assert b.decisions[0]["answers"] == answers
    assert b.decisions[1] == {"ask_id": p["ask_id"], "decision": "deny", "answers": {"_note": "not now"}}


def test_second_answer_is_a_409_already_answered(bridge):
    a = ask(1)
    bridge([a])
    assert A.decide({"ask_id": a["ask_id"], "decision": "approve"})[0] == 200
    code, out = A.decide({"ask_id": a["ask_id"], "decision": "deny"})
    assert code == 409 and out == {"ok": False, "error": "already answered"}


def test_expired_ask_is_a_409_with_its_state(bridge):
    a = ask(1, "expired")
    bridge([a])
    code, out = A.decide({"ask_id": a["ask_id"], "decision": "approve"})
    assert code == 409 and out["state"] == "expired"


@pytest.mark.parametrize("req", [
    {}, {"decision": "approve"}, {"ask_id": "../x", "decision": "approve"},
    {"ask_id": "ask-0000000001", "decision": "maybe"},
    {"ask_id": "ask-0000000001", "decision": "approve", "answers": ["x"]},
    {"ask_id": "ask-0000000001", "decision": "approve", "answers": {"q": ["a"]}}])
def test_bad_requests_never_reach_the_bridge(bridge, req):
    b = bridge([ask(1)])
    assert A.decide(req)[0] == 400 and not b.seen


# ── page ────────────────────────────────────────────────────────────────────────────────────────

def test_page_has_the_columns_buttons_and_never_the_token():
    for col in ("Chat", "Kind", "Tool / summary", "Created", "State"):
        assert f"<th>{col}</th>" in A.PAGE
    for s in ("Approve", "Deny", "Submit", "Other", "already answered", "expired"):
        assert s in A.PAGE
    assert "/api/asks/decide" in A.PAGE and "X-Nomad-Token" not in A.PAGE and "8093" not in A.PAGE
    assert "innerHTML" not in A.PAGE          # ask text is model-written: textContent only


# ── routes (engine image) ───────────────────────────────────────────────────────────────────────

def _client():
    pytest.importorskip("fastapi", reason="engine deps live in the container")
    pytest.importorskip("httpx", reason="TestClient needs httpx")
    from fastapi.testclient import TestClient
    import server
    return server, TestClient(server.app)


def test_routes_list_and_decide_through_the_engine(bridge):
    server, c = _client()
    a = ask(1)
    b = bridge([a, ask(2, "answered")])
    r = c.get("/api/asks?state=pending")
    assert r.status_code == 200 and [x["ask_id"] for x in r.json()["asks"]] == [a["ask_id"]]
    assert c.get("/cockpit/asks").status_code == 200
    r = c.post("/api/asks/decide", json={"ask_id": a["ask_id"], "decision": "approve"})
    assert r.status_code == 200 and b.runs[a["ask_id"]].wait(2)
    r = c.post("/api/asks/decide", json={"ask_id": a["ask_id"], "decision": "approve"})
    assert r.status_code == 409 and r.json()["error"] == "already answered"
    assert c.post("/api/asks/decide", content=b"nope").status_code == 400


def test_decide_is_a_tailnet_write_and_closed_when_exposed():
    pytest.importorskip("fastapi", reason="engine deps live in the container")
    import server
    assert server._tailnet_write_allowed("/api/asks/decide")             # #1407: from the phone
    assert not server._tailnet_write_allowed("/api/asks")
    # the method rule closes every POST while exposed; exercised with the module's mode patched
    old, server.AUTH_MODE = server.AUTH_MODE, "on"
    try:
        assert server._auth_check({}, "/api/asks/decide", "POST")[0] == 403
    finally:
        server.AUTH_MODE = old
