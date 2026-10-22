"""The /policies routes (#1396): the dispatcher's wrapper around nomad_policy/api.py. The logic has
its own tests (nomad_policy/test_api.py); these check the routes, the status codes and the
cockpit token, which is what keeps an agent holding only the dispatch token from saving a
guardrail (A8). Writes go to a throwaway clone, never to the real repo.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest dispatcher/test_policies.py -q
"""
import functools
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("NOMAD_DISPATCH_TOKEN", "test-token")   # before import: TOKEN is module-level

import dispatch  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from nomad_policy import api, schema  # noqa: E402

COCKPIT = "cockpit-t"


@pytest.fixture
def base(monkeypatch, tmp_path):
    if not os.path.isdir(schema.NOMAD):
        pytest.skip("no hub checkout")
    repo = str(tmp_path / "nomad")
    subprocess.run(["git", "clone", "-q", "--shared", "-b", api.MASTER, schema.NOMAD, repo], check=True)
    for name in ("save", "save_guardrail", "attach", "tag_save", "tag_accept"):
        monkeypatch.setattr(api, name, functools.partial(getattr(api, name), root=repo))
    monkeypatch.setattr(dispatch, "COCKPIT_TOKEN", COCKPIT)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dispatch.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", repo
    srv.shutdown()


def _call(url, method, path, body=None, token="test-token", cockpit=None):
    headers = {"Content-Type": "application/json", "X-Nomad-Token": token}
    if cockpit is not None:
        headers["X-Nomad-Cockpit"] = cockpit
    req = urllib.request.Request(url + path, method=method, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _branches(repo):
    return subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout


GUARD = "guardrail-guard-hook"


def _guard_text(url):
    return _call(url, "GET", f"/policies/get?id={GUARD}")[1]["yaml"]


def test_every_policy_route_needs_the_dispatch_token(base):
    url, _ = base
    assert _call(url, "GET", "/policies", token="wrong")[0] == 401
    assert _call(url, "POST", "/policies/save", {"id": "x", "yaml": "x"}, token="wrong")[0] == 401


def test_list_get_and_an_unknown_id(base):
    url, _ = base
    code, got = _call(url, "GET", "/policies")
    assert code == 200 and any(p["id"] == GUARD and p["guardrail"] for p in got["policies"])
    code, got = _call(url, "GET", "/policies/get?id=design-read")
    assert code == 200 and got["policy"]["id"] == "design-read"
    assert _call(url, "GET", "/policies/get?id=nope")[0] == 404


def test_effective_simulate_and_tag_dry_run(base):
    url, _ = base
    code, got = _call(url, "GET", "/policies/effective?project=crypto-trading-bot")
    assert code == 200 and got["layers"]["guardrail"]
    code, got = _call(url, "POST", "/policies/simulate",
                      {"project": "nomad", "kind": "tool", "name": "Bash(git push *)"})
    assert code == 200 and got["effect"] == "deny" and got["reason"] == "explicit-deny"   # deny beats allow
    assert _call(url, "POST", "/policies/simulate", {"project": "nomad", "kind": "planet", "name": "x"})[0] == 400
    code, got = _call(url, "POST", "/policies/tag-dry-run", {"project": "crypto-trading-bot", "type": "website"})
    assert code == 200 and got["can_save"]


def test_tag_save_and_accept_write_the_tag_branch(base):
    """#1398: the tag lands on policy/tag-<project> with every add held; accept lifts one."""
    url, repo = base
    code, got = _call(url, "POST", "/policies/tag-save", {"project": "crypto-trading-bot", "type": "website"})
    assert code == 200 and got["branch"] == "policy/tag-crypto-trading-bot" and got["held"]
    assert _call(url, "POST", "/policies/tag-save", {"project": "crypto-trading-bot", "type": "website"})[0] == 409
    held = got["held"][0]
    assert _call(url, "POST", "/policies/tag-accept", {"project": "crypto-trading-bot", "change": "mcp:+nope"})[0] == 409
    code, got = _call(url, "POST", "/policies/tag-accept", {"project": "crypto-trading-bot", "change": held})
    assert code == 200 and held not in got["held"]
    assert "policy/tag-crypto-trading-bot" in _branches(repo)


def test_validate_checks_and_writes_nothing(base):
    url, repo = base
    text = "id: test-extra\nrole: permission\ndescription: A test policy.\n"
    code, got = _call(url, "POST", "/policies/validate", {"id": "test-extra", "yaml": text})
    assert code == 200 and got["policy"]["description"] == "A test policy."
    assert _call(url, "POST", "/policies/validate", {"id": "test-extra", "yaml": "bogus: 1\n"})[0] == 400
    assert "policy/test-extra" not in _branches(repo)


def test_save_and_attach_write_a_branch(base):
    url, repo = base
    text = "id: test-extra\nrole: permission\ndescription: A test policy.\n"
    code, got = _call(url, "POST", "/policies/save", {"id": "test-extra", "yaml": text})
    assert code == 200 and got["branch"] == "policy/test-extra"
    code, got = _call(url, "POST", "/policies/attach", {"target": "type", "name": "service", "policy": "design-read"})
    assert code == 200 and got["branch"] == "policy/attach-type-service"
    code, _ = _call(url, "POST", "/policies/attach",
                    {"target": "type", "name": "generic", "policy": "design-read", "detach": True})
    assert code == 409
    assert "policy/test-extra" in _branches(repo)


def test_the_ordinary_save_refuses_a_guardrail(base):
    url, repo = base
    code, got = _call(url, "POST", "/policies/save", {"id": GUARD, "yaml": _guard_text(url)})
    assert code == 403 and "guardrail" in got["error"]
    assert f"policy/{GUARD}" not in _branches(repo)


@pytest.mark.parametrize("cockpit", [None, "", "wrong", "test-token"])
def test_the_dispatch_token_alone_cannot_save_a_guardrail(base, cockpit):
    """A8: what an agent holds (the dispatch token, even passed as the cockpit token) is refused."""
    url, repo = base
    body = {"id": GUARD, "yaml": _guard_text(url), "confirm": GUARD}
    code, got = _call(url, "POST", "/policies/guardrail-save", body, cockpit=cockpit)
    assert code == 403 and "cockpit" in got["error"]
    assert f"policy/{GUARD}" not in _branches(repo)


def test_no_cockpit_token_on_the_host_refuses_every_guardrail_save(base, monkeypatch):
    url, _ = base
    monkeypatch.setattr(dispatch, "COCKPIT_TOKEN", "")
    body = {"id": GUARD, "yaml": _guard_text(url), "confirm": GUARD}
    assert _call(url, "POST", "/policies/guardrail-save", body, cockpit="")[0] == 403


def test_the_cockpit_saves_a_guardrail_with_the_typed_confirmation(base):
    url, _ = base
    text = _guard_text(url).replace("Layer 1", "Layer one", 1)
    code, got = _call(url, "POST", "/policies/guardrail-save", {"id": GUARD, "yaml": text, "confirm": "yes"},
                      cockpit=COCKPIT)
    assert code == 403 and "confirm" in got["error"]
    code, got = _call(url, "POST", "/policies/guardrail-save", {"id": GUARD, "yaml": text, "confirm": GUARD},
                      cockpit=COCKPIT)
    assert code == 200 and got["guardrail"] and got["branch"] == f"policy/{GUARD}"


def test_a_bad_body_is_a_400(base):
    url, _ = base
    req = urllib.request.Request(url + "/policies/save", method="POST", data=b"[1",
                                 headers={"X-Nomad-Token": "test-token"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 400
