"""POST /create-project and GET /create-project?id= (#1374): the dispatcher forwards to the
engine's /owui/create-project, where nomad_projects.create runs, and changes nothing itself.

Run: python3 -m pytest dispatcher/test_create_project.py -q
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("NOMAD_DISPATCH_TOKEN", "test-token")   # before import: TOKEN is module-level

import dispatch  # noqa: E402


class FakeEngine(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _answer(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeEngine.seen.append(("POST", self.path, self.headers.get("X-Nomad-Token"), body))
        if not body.get("prd"):
            return self._answer(400, {"ok": False, "id": "tide-log", "error": "needs the PRD"})
        self._answer(202, {"ok": True, "id": "tide-log", "stage": "setup", "running": True})

    def do_GET(self):
        FakeEngine.seen.append(("GET", self.path, self.headers.get("X-Nomad-Token"), None))
        self._answer(200, {"ok": True, "id": "tide-log", "stage": "done", "running": False})


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def servers(monkeypatch):
    FakeEngine.seen = []
    eng, disp = _serve(FakeEngine), _serve(dispatch.Handler)
    monkeypatch.setattr(dispatch, "ENGINE_URL", f"http://127.0.0.1:{eng.server_port}")
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", "owui-t")
    yield f"http://127.0.0.1:{disp.server_port}"
    eng.shutdown()
    disp.shutdown()


def _call(base, method, path, body=None, token="test-token"):
    req = urllib.request.Request(base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Nomad-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_post_forwards_the_answers_with_the_owui_token(servers):
    body = {"name": "Tide Log", "prd": "# PRD", "type": "website", "extra": {"cms": "none"}}
    code, got = _call(servers, "POST", "/create-project", body)
    assert code == 200 and got == {"ok": True, "id": "tide-log", "stage": "setup", "running": True}
    assert FakeEngine.seen == [("POST", "/owui/create-project", "owui-t", body)]


def test_a_refusal_comes_back_as_the_engine_said_it(servers):
    code, got = _call(servers, "POST", "/create-project", {"name": "Tide Log"})
    assert code == 200 and got["ok"] is False and got["error"] == "needs the PRD"


def test_get_reports_the_run(servers):
    code, got = _call(servers, "GET", "/create-project?id=tide-log")
    assert got["stage"] == "done"
    assert FakeEngine.seen[-1][:3] == ("GET", "/owui/create-project/tide-log", "owui-t")
    _, bad = _call(servers, "GET", "/create-project?id=../x")
    assert bad["ok"] is False and len(FakeEngine.seen) == 1


def test_both_need_the_dispatch_token(servers):
    assert _call(servers, "POST", "/create-project", {"name": "x"}, token="wrong")[0] == 401
    assert _call(servers, "GET", "/create-project?id=x", token="wrong")[0] == 401
    assert FakeEngine.seen == []


def test_no_owui_token_or_no_engine_is_a_plain_error(servers, monkeypatch, tmp_path):
    monkeypatch.delenv("NOMAD_OWUI_TOKEN")
    monkeypatch.setattr(dispatch, "OWUI_TOKEN_FILE", str(tmp_path / "missing"))
    _, got = _call(servers, "POST", "/create-project", {"name": "x"})
    assert got["ok"] is False and "no Open WebUI token" in got["error"]
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", "owui-t")
    monkeypatch.setattr(dispatch, "ENGINE_URL", "http://127.0.0.1:9")
    _, got = _call(servers, "POST", "/create-project", {"name": "x"})
    assert got["ok"] is False and "can't be reached" in got["error"]
