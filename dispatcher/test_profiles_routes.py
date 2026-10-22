"""The /profiles/* routes (#1377): the dispatcher's wrapper around nomad_projects/profile_api.py.
The logic has its own tests (nomad_projects/test_profile_api.py); these check the routes, the
status codes and the token (save only with bad profiles: it would commit in the hub), and that the + New project GET /profiles is unchanged.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest dispatcher/test_profiles_routes.py -q
"""
import json
import os
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
from nomad_policy import schema  # noqa: E402


@pytest.fixture(scope="module")
def url():
    if not os.path.isdir(schema.NOMAD):
        pytest.skip("no hub checkout")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dispatch.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _call(url, method, path, body=None, token="test-token", raw=None):
    req = urllib.request.Request(url + path, method=method,
                                 headers={"Content-Type": "application/json", "X-Nomad-Token": token},
                                 data=raw if raw is not None else None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _text(pid):
    with open(os.path.join(dispatch.HUB, "profiles", pid + ".yaml")) as f:
        return f.read()


def test_every_profile_route_needs_the_dispatch_token(url):
    assert _call(url, "GET", "/profiles/list", token="wrong")[0] == 401
    assert _call(url, "POST", "/profiles/dry-run", {"id": "website"}, token="wrong")[0] == 401


def test_list_get_and_an_unknown_id(url):
    code, got = _call(url, "GET", "/profiles/list")
    assert code == 200 and "website" in [p["id"] for p in got["profiles"]] and got["choices"]["policies"]
    code, got = _call(url, "GET", "/profiles/get?id=website")
    assert code == 200 and got["yaml"] == _text("website")
    assert _call(url, "GET", "/profiles/get?id=no-such-type")[0] == 404


def test_the_new_project_menu_is_unchanged(url):
    code, got = _call(url, "GET", "/profiles")
    assert code == 200 and got["ok"] and "interview" in got["profiles"][0]


def test_validate_and_dry_run(url):
    code, got = _call(url, "POST", "/profiles/validate", {"id": "website", "yaml": _text("website")})
    assert code == 200 and got["diff"] == ""
    code, got = _call(url, "POST", "/profiles/validate", {"id": "website", "yaml": "id: website\n"})
    assert code == 400 and "missing required" in got["error"]
    code, got = _call(url, "POST", "/profiles/dry-run", {"id": "website"})
    assert code == 200 and not got["edited"] and got["facts"]
    code, got = _call(url, "POST", "/profiles/dry-run", {"id": "website", "yaml": _text("website")})
    assert code == 200 and got["edited"] and got["changes"] == ""


def test_a_get_dry_run_is_master_only(url):
    code, got = _call(url, "GET", "/profiles/dry-run?id=website&yaml=id%3A%20website%0A")
    assert code == 200 and not got["edited"] and got["changes"] is None
    assert _call(url, "GET", "/profiles/dry-run?id=no-such-type")[0] == 404


def test_save_is_routed_and_refuses_a_bad_profile_before_writing(url):
    # a real save would commit in the hub; nomad_projects/test_profile_api.py saves in a clone
    before = dispatch.subprocess.run(["git", "branch", "--list", "profile/*"], cwd=dispatch.HUB,
                                     capture_output=True, text=True).stdout
    code, got = _call(url, "POST", "/profiles/save", {"id": "website", "yaml": "id: website\n"})
    assert code == 400 and "missing required" in got["error"]
    assert _call(url, "POST", "/profiles/save", {"id": "website"})[0] == 400
    assert _call(url, "POST", "/profiles/save", {"id": "website"}, token="wrong")[0] == 401
    assert dispatch.subprocess.run(["git", "branch", "--list", "profile/*"], cwd=dispatch.HUB,
                                   capture_output=True, text=True).stdout == before


def test_bad_bodies_and_unknown_routes(url):
    assert _call(url, "POST", "/profiles/validate", raw=b"not json")[0] == 400
    assert _call(url, "POST", "/profiles/validate", raw=b"[1]")[0] == 400
    assert _call(url, "POST", "/profiles/delete", {"id": "website"})[0] == 404
    assert _call(url, "GET", "/profiles/nope")[0] == 404
