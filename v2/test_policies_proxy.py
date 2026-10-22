"""#1396 · the engine's /api/policies/* routes, which proxy the dispatcher's /policies routes.

The claims: the dispatch token is added here, the cockpit token goes only with a guardrail save
and is read when the save is made, unknown routes and a dispatcher outage never pass as success,
and from the tailnet the policy layer can be read but not changed.

The dispatcher is faked with a real HTTP server on a loopback port, so what is tested is what
goes over the wire.

Run: python3 -m pytest v2/test_policies_proxy.py -q   (engine deps live in the container)
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

TOKEN = "dispatch-t"
COCKPIT = "cockpit-t"


class Fake:
    """A stand-in dispatcher that records each request."""

    def __init__(self):
        self.seen = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.seen.append({"method": self.command, "path": self.path, "body": body,
                                  "token": self.headers.get("X-Nomad-Token"),
                                  "cockpit": self.headers.get("X-Nomad-Cockpit")})
                code, obj = (404, {"ok": False, "error": "not found"}) if "nope" in self.path \
                    else (200, {"ok": True, "path": self.path})
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            do_GET = do_POST = _answer

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(server, "DISPATCH_URL", f.url)
    monkeypatch.setattr(server, "DISPATCH_TOKEN", TOKEN)
    monkeypatch.setenv("NOMAD_COCKPIT_TOKEN", COCKPIT)
    monkeypatch.setattr(server, "AUTH_MODE", "off")
    yield f
    f.srv.shutdown()


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.mark.parametrize("url, path", [
    ("/api/policies", "/policies"),
    ("/api/policies/get?id=design-read", "/policies/get?id=design-read"),
    ("/api/policies/effective?project=nomad", "/policies/effective?project=nomad"),
])
def test_reads_are_proxied_with_the_dispatch_token_only(fake, client, url, path):
    r = client.get(url)
    assert r.status_code == 200 and r.json()["path"] == path
    assert fake.seen[-1]["token"] == TOKEN and fake.seen[-1]["cockpit"] is None


@pytest.mark.parametrize("name", ["simulate", "tag-dry-run", "tag-save", "tag-accept", "validate", "save", "attach"])
def test_ordinary_writes_never_carry_the_cockpit_token(fake, client, name):
    r = client.post(f"/api/policies/{name}", json={"id": "x"})
    assert r.status_code == 200
    s = fake.seen[-1]
    assert s["path"] == f"/policies/{name}" and s["body"] == {"id": "x"}
    assert s["token"] == TOKEN and s["cockpit"] is None


def test_the_guardrail_save_adds_the_cockpit_token(fake, client):
    body = {"id": "guardrail-guard-hook", "yaml": "id: x\n", "confirm": "guardrail-guard-hook"}
    r = client.post("/api/policies/guardrail-save", json=body)
    assert r.status_code == 200
    assert fake.seen[-1]["cockpit"] == COCKPIT and fake.seen[-1]["body"] == body


def test_the_cockpit_token_is_read_at_save_time(fake, client, monkeypatch):
    monkeypatch.setenv("NOMAD_COCKPIT_TOKEN", "rotated")
    client.post("/api/policies/guardrail-save", json={})
    assert fake.seen[-1]["cockpit"] == "rotated"


def test_unknown_routes_are_404_and_never_reach_the_dispatcher(fake, client):
    assert client.get("/api/policies/everything").status_code == 404
    assert client.post("/api/policies/delete", json={}).status_code == 404
    assert fake.seen == []


def test_the_dispatcher_status_is_kept(fake, client):
    assert client.get("/api/policies/get?id=nope").status_code == 404


def test_a_dispatcher_outage_is_a_502(fake, client, monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(server, "DISPATCH_URL", f"http://127.0.0.1:{port}")
    r = client.get("/api/policies")
    assert r.status_code == 502 and "dispatcher link error" in r.json()["error"]


def test_from_the_tailnet_policies_are_read_only(fake, client, monkeypatch):
    op, origin = "jde@example.com", "https://nomad.tail1234.ts.net:8449"
    monkeypatch.setattr(server, "AUTH_MODE", "tailnet")
    monkeypatch.setattr(server, "TAILNET_OPERATOR", op)
    monkeypatch.setattr(server, "TAILNET_ORIGIN", origin)
    h = {"x-forwarded-for": "100.64.0.7", "tailscale-user-login": op, "origin": origin}
    assert client.get("/api/policies", headers=h).status_code == 200
    for name in ("save", "attach", "guardrail-save", "simulate", "validate", "tag-save", "tag-accept"):
        assert client.post(f"/api/policies/{name}", json={}, headers=h).status_code == 403
    assert all(s["method"] == "GET" for s in fake.seen)


# ── /api/profiles/* (#1377): the Standards area, proxied the same way ──

@pytest.mark.parametrize("url, path", [
    ("/api/profiles/list", "/profiles/list"),
    ("/api/profiles/get?id=website", "/profiles/get?id=website"),
])
def test_profile_reads_are_proxied_with_the_dispatch_token_only(fake, client, url, path):
    r = client.get(url)
    assert r.status_code == 200 and r.json()["path"] == path
    assert fake.seen[-1]["token"] == TOKEN and fake.seen[-1]["cockpit"] is None


@pytest.mark.parametrize("name", ["validate", "dry-run", "save"])
def test_profile_posts_forward_the_body_without_the_cockpit_token(fake, client, name):
    body = {"id": "website", "yaml": "id: website\n"}
    r = client.post(f"/api/profiles/{name}", json=body)
    assert r.status_code == 200 and r.json()["path"] == f"/profiles/{name}"
    assert fake.seen[-1]["body"] == body and fake.seen[-1]["cockpit"] is None


def test_unknown_profile_routes_never_reach_the_dispatcher(fake, client):
    assert client.get("/api/profiles/nope").status_code == 404
    assert client.post("/api/profiles/delete", json={}).status_code == 404
    assert client.post("/api/profiles/guardrail-save", json={}).status_code == 404
    assert fake.seen == []


def test_a_get_dry_run_forwards_only_the_id(fake, client):
    r = client.get("/api/profiles/dry-run?id=website&yaml=id%3A%20x")
    assert r.status_code == 200 and fake.seen[-1]["path"] == "/profiles/dry-run?id=website"
    assert fake.seen[-1]["method"] == "GET"


def test_from_the_tailnet_profiles_are_read_only(fake, client, monkeypatch):
    op, origin = "jde@example.com", "https://nomad.tail1234.ts.net:8449"
    monkeypatch.setattr(server, "AUTH_MODE", "tailnet")
    monkeypatch.setattr(server, "TAILNET_OPERATOR", op)
    monkeypatch.setattr(server, "TAILNET_ORIGIN", origin)
    h = {"x-forwarded-for": "100.64.0.7", "tailscale-user-login": op, "origin": origin}
    assert client.get("/api/profiles/list", headers=h).status_code == 200
    assert client.get("/api/profiles/dry-run?id=website", headers=h).status_code == 200
    for name in ("validate", "dry-run", "save"):
        assert client.post(f"/api/profiles/{name}", json={}, headers=h).status_code == 403
    assert all(s["method"] == "GET" for s in fake.seen)
