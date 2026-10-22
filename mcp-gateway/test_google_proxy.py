import gzip
import json
import os
import sys
import tempfile
import time

import httpx
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))
import google_proxy as gp

KEY = "k" * 32


class _Body(httpx.AsyncByteStream):
    def __init__(self, b: bytes):
        self.b = b

    async def __aiter__(self):
        yield self.b


def streamed(status: int, body: bytes, headers: dict | None = None) -> httpx.Response:
    """A response that is still unread, like a real upstream one (Response(content=...) is pre-read)."""
    return httpx.Response(status, headers=headers or {}, stream=_Body(body))


class World:
    """Fake vault + Google. Counts calls; `expire_once` makes the next MCP call answer 401."""

    def __init__(self):
        self.refreshes = self.logins = self.mcp_calls = 0
        self.expire_once = False
        self.seen_auth = []
        self.vault_ok = True
        self.expires_in = 3599

    def handler(self, req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u.endswith("/v1/auth/approle/login"):
            self.logins += 1
            if not self.vault_ok:
                return httpx.Response(503, json={"errors": ["sealed"]})
            return httpx.Response(200, json={"auth": {"client_token": "vt", "lease_duration": 3600}})
        if "/v1/secret/data/google/local" in u:
            assert req.headers["x-vault-token"] == "vt"
            return httpx.Response(200, json={"data": {"data": {
                "client_id": "cid", "client_secret": "csec", "refresh_token": "rt"}}})
        if u == gp.TOKEN_URL:
            self.refreshes += 1
            body = req.content.decode()
            assert "refresh_token=rt" in body and "client_id=cid" in body
            return httpx.Response(200, json={"access_token": f"at{self.refreshes}", "expires_in": self.expires_in})
        if "mcp.googleapis.com" in u:
            self.mcp_calls += 1
            self.seen_auth.append(req.headers["authorization"])
            if self.expire_once:
                self.expire_once = False
                return streamed(401, b'{"error": "expired"}')
            return streamed(200, b'data: {"ok":1}\n\n', {"content-type": "text/event-stream"})
        return httpx.Response(404)


def app(w: World):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "ar.json")
    json.dump({"role_id": "r", "secret_id": "s"}, open(p, "w"))
    return TestClient(gp.build(KEY, "http://v", p, transport=httpx.MockTransport(w.handler)))


H = {"Authorization": f"Bearer {KEY}"}


def test_requires_the_wrapper_bearer():
    w = World(); c = app(w)
    assert c.post("/gmail/mcp").status_code == 401
    assert c.post("/gmail/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert w.mcp_calls == 0


def test_forwards_with_a_fresh_token_and_streams_the_body():
    w = World(); c = app(w)
    r = c.post("/drive/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200 and r.content == b"data: {\"ok\":1}\n\n"
    assert w.seen_auth == ["Bearer at1"]          # caller's bearer is not forwarded


def test_token_is_cached_between_calls():
    w = World(); c = app(w)
    for _ in range(3):
        assert c.post("/gmail/mcp", headers=H).status_code == 200
    assert w.refreshes == 1 and w.logins == 1


def test_token_near_expiry_is_refreshed():
    w = World(); w.expires_in = 100; c = app(w)       # under the 5 minute skew
    c.post("/gmail/mcp", headers=H); c.post("/gmail/mcp", headers=H)
    assert w.refreshes == 2


def test_one_401_refreshes_once_and_retries_once():
    w = World(); c = app(w)
    c.post("/calendar/mcp", headers=H)
    w.expire_once = True
    r = c.post("/calendar/mcp", headers=H)
    assert r.status_code == 200 and w.refreshes == 2 and w.seen_auth[-2:] == ["Bearer at1", "Bearer at2"]


def test_persistent_401_is_returned_not_looped():
    w = World()
    orig = w.handler

    def always401(req):
        if "mcp.googleapis.com" in str(req.url):
            w.mcp_calls += 1
            return streamed(401, b'{"error": "no"}')
        return orig(req)
    d = tempfile.mkdtemp()
    p = os.path.join(d, "a.json")
    json.dump({"role_id": "r", "secret_id": "s"}, open(p, "w"))
    c = TestClient(gp.build(KEY, "http://v", p, transport=httpx.MockTransport(always401)))
    assert c.post("/gmail/mcp", headers=H).status_code == 401 and w.mcp_calls == 2


def test_sealed_vault_is_a_503_and_leaks_nothing():
    w = World(); w.vault_ok = False; c = app(w)
    r = c.post("/drive/mcp", headers=H)
    assert r.status_code == 503 and r.json()["error"] == "google token unavailable"
    for s in ("csec", "refresh", KEY, "vt"):
        assert s not in r.text


def test_unknown_service_and_health():
    w = World(); c = app(w)
    assert c.post("/slack/mcp", headers=H).status_code == 404
    assert c.get("/health").json() == {"ok": True}


def test_content_encoding_travels_with_the_raw_bytes():
    w = World()
    orig = w.handler

    def gz(req):
        if "mcp.googleapis.com" in str(req.url):
            return streamed(200, gzip.compress(b'{"ok": 1}'), {"content-encoding": "gzip", "content-type": "application/json"})
        return orig(req)
    d = tempfile.mkdtemp()
    p = os.path.join(d, "a.json")
    json.dump({"role_id": "r", "secret_id": "s"}, open(p, "w"))
    c = TestClient(gp.build(KEY, "http://v", p, transport=httpx.MockTransport(gz)))
    r = c.post("/gmail/mcp", headers={**H, "Accept-Encoding": "identity"})
    assert r.headers["content-encoding"] == "gzip" and r.json() == {"ok": 1}
