"""AP-15 (#882) · the engine's /api/autopilot/* routes, which proxy the autopilot supervisor.

The claims worth checking: the token is added here and never needed from the browser, a write is
refused whenever the engine's auth says writes are closed, a supervisor that is down is a 503 the
cockpit can print rather than a stack trace, and a bad project id or stop mode never reaches the
supervisor at all.

The supervisor is faked with a real HTTP server on a free loopback port, so what is tested is what
goes over the wire: path, method, header, body.

Run: python3 -m pytest v2/test_autopilot_proxy.py -q   (engine deps live in the container)
"""
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

TOKEN = "sekrit-autopilot-token"
STATUS = {"ok": True, "heartbeat_age_s": 1.0, "config": {"budget_mode": "cap"},
          "spend": {}, "projects": {"runematch": {"enabled": True, "state": "running"}}}
RUNS = [{"project": "runematch", "task": "648", "outcome": "running", "started": 1.0}]


class Fake:
    """A stand-in supervisor that records every request and answers like the real one."""

    def __init__(self):
        self.seen = []
        self.reply = None           # (code, obj) to override the next answer
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _record(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.seen.append({"method": self.command, "path": self.path,
                                  "token": self.headers.get("X-Nomad-Token"), "body": body})

            def do_GET(self):
                self._record()
                if fake.reply:
                    return self._send(*fake.reply)
                if self.path == "/status":
                    return self._send(200, STATUS)
                if self.path == "/config":
                    return self._send(200, {"ok": True, "config": STATUS["config"]})
                if self.path.startswith("/runs"):
                    return self._send(200, {"ok": True, "runs": RUNS})
                self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                self._record()
                if self.headers.get("X-Nomad-Token") != TOKEN:
                    return self._send(401, {"ok": False, "error": "missing or wrong X-Nomad-Token"})
                if fake.reply:
                    return self._send(*fake.reply)
                if self.path == "/pass":
                    return self._send(202, {"ok": True, "queued": True})
                self._send(200, {"ok": True, "echo": self.path})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def posts(self):
        return [s for s in self.seen if s["method"] == "POST"]


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(server, "AUTOPILOT_URL", f.url)
    monkeypatch.setenv("NOMAD_AUTOPILOT_TOKEN", TOKEN)
    monkeypatch.setattr(server, "AUTOPILOT_TIMEOUT", 2)
    monkeypatch.setattr(server, "_registry_ids", lambda: ["runematch", "nomad", "civis-poc"])
    monkeypatch.setattr(server, "AUTH_MODE", "off")
    yield f
    f.srv.shutdown()


@pytest.fixture
def client():
    return TestClient(server.app)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── proxying and the token ──────────────────────────────────────────────────────────────────────

def test_status_is_proxied(fake, client):
    r = client.get("/api/autopilot/status")
    assert r.status_code == 200 and r.json() == STATUS
    assert fake.seen[-1]["path"] == "/status"


def test_config_read_is_proxied(fake, client):
    r = client.get("/api/autopilot/config")
    assert r.status_code == 200 and r.json()["config"] == {"budget_mode": "cap"}


def test_runs_are_proxied_without_a_token_and_the_limit_is_clamped(fake, client):
    r = client.get("/api/autopilot/runs")
    assert r.status_code == 200 and r.json()["runs"] == RUNS
    assert fake.seen[-1]["path"] == "/runs?limit=50" and fake.seen[-1]["token"] is None
    client.get("/api/autopilot/runs?limit=5000")
    assert fake.seen[-1]["path"] == "/runs?limit=200"
    assert client.get("/api/autopilot/runs?limit=many").status_code == 422


def test_start_adds_the_token_the_browser_never_sent(fake, client):
    r = client.post("/api/autopilot/projects/runematch/start")
    assert r.status_code == 200, r.text
    p = fake.posts()
    assert [x["path"] for x in p] == ["/projects/runematch/start"]
    assert p[0]["token"] == TOKEN


def test_the_token_never_comes_back_to_the_browser(fake, client):
    for r in (client.get("/api/autopilot/status"),
              client.post("/api/autopilot/projects/runematch/start"),
              client.post("/api/autopilot/pass")):
        assert TOKEN not in r.text
        assert TOKEN not in json.dumps(dict(r.headers))


def test_stop_defaults_to_finish(fake, client):
    assert client.post("/api/autopilot/projects/runematch/stop").status_code == 200
    assert fake.posts()[-1] == {"method": "POST", "path": "/projects/runematch/stop",
                                "token": TOKEN, "body": {"mode": "finish"}}


def test_stop_kill_is_passed_through(fake, client):
    assert client.post("/api/autopilot/projects/runematch/stop",
                       json={"mode": "kill"}).status_code == 200
    assert fake.posts()[-1]["body"] == {"mode": "kill"}


def test_pass_is_proxied_with_its_202(fake, client):
    r = client.post("/api/autopilot/pass")
    assert r.status_code == 202 and r.json()["queued"] is True
    assert fake.posts()[-1]["path"] == "/pass" and fake.posts()[-1]["token"] == TOKEN


def test_config_write_forwards_the_body(fake, client):
    r = client.post("/api/autopilot/config", json={"budget_mode": "unlimited"})
    assert r.status_code == 200
    assert fake.posts()[-1]["body"] == {"budget_mode": "unlimited"}


def test_config_write_needs_an_object(fake, client):
    for body in ([], {}, "x"):
        assert client.post("/api/autopilot/config", json=body).status_code == 400
    assert fake.posts() == []


def test_supervisor_errors_pass_through(fake, client):
    fake.reply = (400, {"ok": False, "error": "unknown config key 'bogus'"})
    r = client.post("/api/autopilot/config", json={"bogus": 1})
    assert r.status_code == 400 and "bogus" in r.json()["error"]


def test_the_token_is_read_per_call_not_at_import(fake, client, monkeypatch, tmp_path):
    """The supervisor writes its token on first start, which can be after the engine's."""
    monkeypatch.delenv("NOMAD_AUTOPILOT_TOKEN")
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "autopilot-token")
                        if p.endswith("autopilot-token") else p)
    real_open = open

    def fake_open(path, *a, **k):
        if str(path).startswith(("/term-secret", "/host/")):
            raise FileNotFoundError(path)
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", fake_open)
    r = client.post("/api/autopilot/pass")
    assert r.status_code == 503 and "token" in r.json()["error"]
    assert fake.posts() == []
    (tmp_path / "autopilot-token").write_text(TOKEN + "\n")
    assert client.post("/api/autopilot/pass").status_code == 202
    assert fake.posts()[-1]["token"] == TOKEN


def test_a_wrong_token_is_not_a_401_to_the_browser(fake, client, monkeypatch):
    """A 401 would read as the browser's own login failing."""
    monkeypatch.setenv("NOMAD_AUTOPILOT_TOKEN", "stale")
    r = client.post("/api/autopilot/pass")
    assert r.status_code == 502 and "token" in r.json()["error"]


# ── bad ids and modes never reach the supervisor ────────────────────────────────────────────────

@pytest.mark.parametrize("pid", ["..", "-rf", "a" * 65, "x y", "a%2Fb"])
def test_a_malformed_id_is_a_400(fake, client, pid):
    r = client.post(f"/api/autopilot/projects/{pid}/start")
    assert r.status_code in (400, 404)
    assert fake.posts() == []


def test_an_unknown_project_is_a_404(fake, client):
    r = client.post("/api/autopilot/projects/not-a-project/start")
    assert r.status_code == 404 and "unknown project" in r.json()["error"]
    assert fake.posts() == []


def test_a_project_only_in_supervisor_state_can_still_be_stopped(fake, client, monkeypatch):
    monkeypatch.setattr(server, "_registry_ids", lambda: ["nomad"])
    assert client.post("/api/autopilot/projects/runematch/stop").status_code == 200


def test_registry_down_and_id_unknown_to_the_supervisor_is_a_503(fake, client, monkeypatch):
    monkeypatch.setattr(server, "_registry_ids", lambda: None)
    r = client.post("/api/autopilot/projects/civis-poc/start")
    assert r.status_code == 503 and "registry" in r.json()["error"]
    assert fake.posts() == []


@pytest.mark.parametrize("body", [{"mode": "now"}, {"mode": ""}, {"mode": None}, ["kill"]])
def test_a_bad_stop_mode_is_a_400(fake, client, body):
    r = client.post("/api/autopilot/projects/runematch/stop", json=body)
    assert r.status_code == 400 and "mode" in r.json()["error"]
    assert fake.posts() == []


# ── the ON/OFF switch (#890) ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("on", [True, False])
def test_the_switch_is_proxied_with_the_token(fake, client, on):
    r = client.post("/api/autopilot/projects/runematch/autopilot", json={"on": on})
    assert r.status_code == 200, r.text
    assert fake.posts() == [{"method": "POST", "path": "/projects/runematch/autopilot",
                             "token": TOKEN, "body": {"on": on}}]


@pytest.mark.parametrize("body", [{}, {"on": "true"}, {"on": 1}, {"on": None}, [True], "on"])
def test_a_bad_switch_body_is_a_400(fake, client, body):
    r = client.post("/api/autopilot/projects/runematch/autopilot", json=body)
    assert r.status_code == 400 and "on" in r.json()["error"]
    assert fake.posts() == []


def test_the_switch_checks_the_project_id(fake, client):
    assert client.post("/api/autopilot/projects/not-a-project/autopilot",
                       json={"on": True}).status_code == 404
    assert client.post("/api/autopilot/projects/-rf/autopilot", json={"on": True}).status_code in (400, 404)
    assert fake.posts() == []


def test_only_the_switch_field_is_forwarded(fake, client):
    client.post("/api/autopilot/projects/runematch/autopilot", json={"on": True, "mode": "kill"})
    assert fake.posts()[-1]["body"] == {"on": True}


# ── supervisor down ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("call", [
    ("get", "/api/autopilot/status", None),
    ("get", "/api/autopilot/config", None),
    ("post", "/api/autopilot/pass", None),
    ("post", "/api/autopilot/config", {"budget_mode": "cap"}),
    ("post", "/api/autopilot/projects/runematch/start", None),
    ("post", "/api/autopilot/projects/runematch/stop", {"mode": "kill"}),
    ("post", "/api/autopilot/projects/runematch/autopilot", {"on": True}),
])
def test_supervisor_down_is_a_clear_503(monkeypatch, client, call):
    monkeypatch.setattr(server, "AUTOPILOT_URL", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setenv("NOMAD_AUTOPILOT_TOKEN", TOKEN)
    monkeypatch.setattr(server, "_registry_ids", lambda: ["runematch"])
    monkeypatch.setattr(server, "AUTH_MODE", "off")
    method, path, body = call
    r = getattr(client, method)(path, **({"json": body} if body else {}))
    assert r.status_code == 503
    assert r.json()["ok"] is False and r.json()["error"] == "autopilot service not running"


def test_a_wedged_supervisor_times_out_to_a_503(monkeypatch, client):
    """Accepts the connection and never answers: the short timeout must turn it into a 503."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        monkeypatch.setattr(server, "AUTOPILOT_URL", f"http://127.0.0.1:{s.getsockname()[1]}")
        monkeypatch.setattr(server, "AUTOPILOT_TIMEOUT", 0.5)
        monkeypatch.setattr(server, "AUTH_MODE", "off")
        r = client.get("/api/autopilot/status")
        assert r.status_code == 503 and r.json()["error"] == "autopilot service not running"
    finally:
        s.close()


# ── the engine's auth rules ─────────────────────────────────────────────────────────────────────

WRITES = [("/api/autopilot/pass", None), ("/api/autopilot/config", {"budget_mode": "unlimited"}),
          ("/api/autopilot/projects/runematch/start", None),
          ("/api/autopilot/projects/runematch/autopilot", {"on": False}),
          ("/api/autopilot/projects/runematch/stop", {"mode": "kill"})]


@pytest.mark.parametrize("path,body", WRITES)
def test_published_engine_refuses_every_write(fake, client, monkeypatch, path, body):
    monkeypatch.setattr(server, "AUTH_MODE", "on")
    monkeypatch.setattr(server, "AUTH_USER", "jde")
    monkeypatch.setattr(server, "AUTH_PASS", "pw")
    r = client.post(path, json=body, auth=("jde", "pw"))
    assert r.status_code == 403
    assert fake.posts() == []


def test_published_engine_still_serves_the_status_with_a_login(fake, client, monkeypatch):
    monkeypatch.setattr(server, "AUTH_MODE", "on")
    monkeypatch.setattr(server, "AUTH_USER", "jde")
    monkeypatch.setattr(server, "AUTH_PASS", "pw")
    assert client.get("/api/autopilot/status").status_code == 401
    assert client.get("/api/autopilot/status", auth=("jde", "pw")).status_code == 200


TAILNET = {"tailscale-user-login": "op@example.com", "origin": "https://cockpit.ts.net"}


@pytest.mark.parametrize("path,body", WRITES)
def test_tailnet_proxy_passes_autopilot_writes_from_the_operator(fake, client, monkeypatch, path, body):
    """#883, operator decision 2026-09-28: Run, Stop, a pass and the budget work from the phone."""
    monkeypatch.setattr(server, "AUTH_MODE", "tailnet")
    monkeypatch.setattr(server, "TAILNET_OPERATOR", "op@example.com")
    monkeypatch.setattr(server, "TAILNET_ORIGIN", "https://cockpit.ts.net")
    assert client.post(path, json=body, headers=TAILNET).status_code in (200, 202)
    assert [p["path"] for p in fake.posts()] == [path.removeprefix("/api/autopilot")]
    assert fake.posts()[0]["token"] == TOKEN


@pytest.mark.parametrize("headers", [{**TAILNET, "origin": "https://evil.example"},
                                     {**TAILNET, "tailscale-user-login": "someone@example.com"},
                                     {"tailscale-user-login": "op@example.com"}])
@pytest.mark.parametrize("path,body", WRITES)
def test_tailnet_proxy_refuses_autopilot_writes_from_anyone_else(fake, client, monkeypatch,
                                                                 path, body, headers):
    monkeypatch.setattr(server, "AUTH_MODE", "tailnet")
    monkeypatch.setattr(server, "TAILNET_OPERATOR", "op@example.com")
    monkeypatch.setattr(server, "TAILNET_ORIGIN", "https://cockpit.ts.net")
    assert client.post(path, json=body, headers=headers).status_code == 403
    assert fake.posts() == []


def test_tailnet_proxy_serves_the_status(fake, client, monkeypatch):
    monkeypatch.setattr(server, "AUTH_MODE", "tailnet")
    monkeypatch.setattr(server, "TAILNET_OPERATOR", "op@example.com")
    monkeypatch.setattr(server, "TAILNET_ORIGIN", "https://cockpit.ts.net")
    assert client.get("/api/autopilot/status", headers=TAILNET).status_code == 200


def test_off_mode_refuses_a_proxied_write(fake, client):
    r = client.post("/api/autopilot/pass", headers={"x-forwarded-for": "203.0.113.7"})
    assert r.status_code == 403 and fake.posts() == []


# ── approve or reject a task in Awaiting approval (#911, docs/prd/autopilot-approval.md 5.3) ────

class FakeOP:
    def __init__(self, status="Awaiting approval"):
        self.status, self.moves = status, []

    def get(self, i):
        if i == 404:
            raise server.PMError("GET /work_packages/404 -> 404: not found")
        return {"id": i, "_links": {"status": {"title": self.status}}}

    def move(self, i, target, comment="", as_=None):
        self.moves.append((i, target, comment, as_))


@pytest.fixture
def op(monkeypatch):
    o = FakeOP()
    monkeypatch.setattr(server.eng, "_op", o)
    monkeypatch.setattr(server, "AUTH_MODE", "off")
    return o


def test_approve_moves_the_task_with_the_answer_as_nomad_manager(op, client):
    r = client.post("/api/autopilot/tasks/647/decide", json={"decision": "approve", "text": " (a) "})
    assert r.status_code == 200 and r.json() == {"ok": True, "task": 647, "status": "Approved"}
    assert op.moves == [(647, "Approved", "**Operator, via the cockpit:**\n\n(a)", "nomad-manager")]


def test_approve_without_text_posts_no_comment(op, client):
    assert client.post("/api/autopilot/tasks/647/decide", json={"decision": "approve"}).status_code == 200
    assert op.moves == [(647, "Approved", "", "nomad-manager")]


def test_reject_needs_a_reason(op, client):
    r = client.post("/api/autopilot/tasks/647/decide", json={"decision": "reject", "text": "  "})
    assert r.status_code == 400 and op.moves == []
    r = client.post("/api/autopilot/tasks/647/decide", json={"decision": "reject", "text": "split it"})
    assert r.json()["status"] == "Changes requested"
    assert op.moves[0][1:3] == ("Changes requested", "**Operator, via the cockpit:**\n\nsplit it")


@pytest.mark.parametrize("body", [{}, {"decision": "maybe"}, {"decision": "approve", "text": 3}])
def test_a_bad_decision_is_a_400(op, client, body):
    assert client.post("/api/autopilot/tasks/647/decide", json=body).status_code == 400
    assert op.moves == []


def test_an_unknown_task_is_a_404_and_a_decided_one_a_409(op, client):
    assert client.post("/api/autopilot/tasks/404/decide", json={"decision": "approve"}).status_code == 404
    op.status = "Approved"
    r = client.post("/api/autopilot/tasks/647/decide", json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["status"] == "Approved" and op.moves == []


def test_decide_is_a_write(op, client, monkeypatch):
    monkeypatch.setattr(server, "AUTH_MODE", "on")
    monkeypatch.setattr(server, "AUTH_USER", "jde")
    monkeypatch.setattr(server, "AUTH_PASS", "pw")
    r = client.post("/api/autopilot/tasks/647/decide", json={"decision": "approve"}, auth=("jde", "pw"))
    assert r.status_code == 403 and op.moves == []
    monkeypatch.setattr(server, "AUTH_MODE", "tailnet")
    monkeypatch.setattr(server, "TAILNET_OPERATOR", "op@example.com")
    monkeypatch.setattr(server, "TAILNET_ORIGIN", "https://cockpit.ts.net")
    assert client.post("/api/autopilot/tasks/647/decide", json={"decision": "approve"},
                       headers={**TAILNET, "origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/autopilot/tasks/647/decide", json={"decision": "approve"},
                       headers=TAILNET).status_code == 200
    assert len(op.moves) == 1
