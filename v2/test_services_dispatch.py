"""V2c · the last two console routes: service health, and handing a task to the Builder.

Service health is the interesting one. The console probed 17 targets itself, live, on every poll.
The engine does not, and the reason is in CLAUDE.md: nomad-watch is a oneshot on a timer precisely
so a wedged watchdog cannot be the outage it exists to report, and a prober living inside a
long-running server gives that property back. Two target lists would also drift, and the drift shows
up as one surface calling a service down while the other calls it up.

So the engine reports nomad-watch's state file instead. That makes the file's age the thing that
matters: a two-hour-old all-clear is exactly the reassuring-but-dead reading nomad-watch was built
to catch, and reporting it as green would rebuild the bug one layer up. Most of these tests are
about that.

Run: python3 -m pytest v2/test_services_dispatch.py -q   (engine deps live in the container)
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402


STATE = {
    "LiteLLM gateway": {"checked": "2026-09-23T22:37:05", "detail": "200", "fails": 0,
                        "ok": True, "since": None},
    "nomad-detect": {"checked": "2026-09-23T22:37:05", "detail": "connection refused", "fails": 3,
                     "ok": False, "since": "2026-09-23T20:10:00"},
    "_meta": {"digest_day": "2026-09-23"},
}


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A watch state file on disk, so the age is the file's real mtime like it is in production."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps(STATE))
    monkeypatch.setattr(server, "WATCH_STATE", str(p))
    monkeypatch.setattr(server, "WATCH_STALE_S", 900)
    return p


def age(p, seconds):
    """Backdate the file. Staleness is measured from mtime, so this is how you make it old."""
    t = time.time() - seconds
    os.utime(p, (t, t))


# ── reading a fresh pass ────────────────────────────────────────────────────────────────────────

def test_a_fresh_pass_reports_each_target(state):
    d = server._services()
    assert [s["name"] for s in d["services"]] == ["LiteLLM gateway", "nomad-detect"]
    assert d["stale"] is False
    assert d.get("error") is None


def test_meta_is_not_a_service(state):
    """`_meta` holds the digest bookkeeping. Rendered as a target it would be a service that is
    permanently down, because it has no `ok` key."""
    assert "_meta" not in [s["name"] for s in server._services()["services"]]


def test_a_down_service_carries_when_it_went_down(state):
    d = server._services()
    bad = [s for s in d["services"] if s["name"] == "nomad-detect"][0]
    assert bad["up"] is False
    assert bad["down_since"] == "2026-09-23T20:10:00"
    assert bad["detail"] == "connection refused"
    assert bad["fails"] == 3


def test_down_since_comes_from_the_prober_not_from_this_read(state):
    """A service down for two hours must not look like it just failed. The prober owns that fact;
    this read has no way to know it."""
    age(state, 60)
    assert server._services()["services"][1]["down_since"] == "2026-09-23T20:10:00"


# ── the age of the pass, which is the whole point ───────────────────────────────────────────────

def test_age_is_measured_from_mtime_not_from_the_timestamp_inside(state):
    """nomad-watch stamps a naive local timestamp and this runs in a UTC container, so parsing that
    string gave an age offset by the timezone. It read -7088s on the real file. A staleness check
    that can go negative is not checking anything, and mtime is the same fact with no timezone."""
    age(state, 300)
    d = server._services()
    assert 290 <= d["age_s"] <= 320
    assert d["stale"] is False


def test_an_old_pass_is_stale(state):
    age(state, 3600)
    assert server._services()["stale"] is True


def test_a_stale_pass_reports_up_as_unknown_not_as_what_it_last_was(state):
    """The reading is a memory, not an observation. `None` forces every consumer to decide what to
    do about unknown; leaving `True` there lets one paint it green and call that health."""
    age(state, 3600)
    for s in server._services()["services"]:
        assert s["up"] is None


def test_a_stale_pass_still_says_what_it_last_saw(state):
    """Unknown is not the same as no information. `last_up` is what the final pass found, and it is
    the right thing to show greyed out next to the word unknown."""
    age(state, 3600)
    d = {s["name"]: s for s in server._services()["services"]}
    assert d["LiteLLM gateway"]["last_up"] is True
    assert d["nomad-detect"]["last_up"] is False


def test_a_stale_pass_says_so_in_words(state):
    age(state, 3600)
    d = server._services()
    assert "not current" in d["error"]
    assert "3" in d["error"]                      # the age is in the message, not just a flag


def test_the_boundary_is_three_missed_passes(state):
    """The timer is every five minutes, so one slipped pass is normal and three is a problem."""
    age(state, 899)
    assert server._services()["stale"] is False
    age(state, 901)
    assert server._services()["stale"] is True


# ── when the file is not there ──────────────────────────────────────────────────────────────────

def test_a_missing_state_file_is_an_error_not_an_empty_list(monkeypatch, tmp_path):
    """An empty list reads as "there are no services", which looks calm. The truth is that nobody
    is watching, which is the loudest thing this endpoint can have to say."""
    monkeypatch.setattr(server, "WATCH_STATE", str(tmp_path / "nope.json"))
    d = server._services()
    assert d["services"] == []
    assert "nomad-watch" in d["error"]


def test_a_corrupt_state_file_says_unreadable(monkeypatch, tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    monkeypatch.setattr(server, "WATCH_STATE", str(p))
    d = server._services()
    assert d["services"] == []
    assert "unreadable" in d["error"]


def test_the_engine_does_not_probe_anything_itself():
    """If this ever grows a request, there are two target lists again and they will disagree. The
    argument against it is in CLAUDE.md and is about watchdogs, not about effort."""
    import inspect
    body = inspect.getsource(server._services)
    body = body.split('"""')[-1]                  # the docstring explains all this; skip it
    for forbidden in ("requests.", "urlopen", "httpx", "socket.", "subprocess"):
        assert forbidden not in body, f"_services reaches out with {forbidden}"


# ── dispatch ────────────────────────────────────────────────────────────────────────────────────

def test_dispatch_sends_the_token(monkeypatch):
    """Every dispatcher route but /health needs it, so a proxy that forgets it turns every build
    into a 401 that looks like the dispatcher being broken."""
    seen = {}

    class R:
        def read(self): return b'{"ok": true}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data)
        return R()

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_open)
    status, out = server._dispatch({"project": "civis", "task": "add a test", "mode": "build"})
    assert status == 200 and out == {"ok": True}
    assert seen["url"].endswith("/dispatch")
    assert seen["headers"]["x-nomad-token"] == server.DISPATCH_TOKEN
    assert seen["body"]["task"] == "add a test"


def test_a_dispatcher_outage_is_a_502_not_a_200(monkeypatch):
    """The console answered 200 with an error in the body. A caller that checked the status code
    alone read a dispatcher that was down as a build that ran."""
    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(server.urllib.request, "urlopen", boom)
    status, out = server._dispatch({"project": "civis", "task": "x"})
    assert status == 502
    assert out["ok"] is False
    assert "connection refused" in out["error"]


def test_the_dispatchers_own_status_code_survives(monkeypatch):
    """Up-and-refusing and down need different things done about them, so they must not collapse
    into one error."""
    def refuse(req, timeout=None):
        raise server.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(server.urllib.request, "urlopen", refuse)
    status, out = server._dispatch({"project": "civis", "task": "x"})
    assert status == 401
    assert "401" in out["error"]


# ── the routes exist and are shaped the way the extension expects ───────────────────────────────

@pytest.mark.parametrize("path,methods", [("/api/services", {"GET"}), ("/api/dispatch", {"POST"})])
def test_the_ported_routes_exist(path, methods):
    got = set()
    for r in server.app.routes:
        if getattr(r, "path", "") == path:
            got |= set(r.methods) - {"HEAD", "OPTIONS"}
    assert got == methods, f"{path} serves {got}"
