"""Tests for nomad-watch.

The watchdog exists because three outages were absences of a signal. Its own failure mode is the
same shape: alert on every pass and the operator mutes the channel, alert on none and it is
decoration. So what is tested here is mostly the *noise* behaviour — when it speaks and when it
stays quiet — plus the one probe that can tell "running" from "working".

Run: python3 -m pytest nomad-watch/test_watch.py -q
"""
import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watch  # noqa: E402


class _Probe:
    """A target whose result the test drives directly."""

    def __init__(self):
        self.ok, self.detail = True, "fine"

    def install(self, monkeypatch):
        monkeypatch.setitem(watch.PROBES, "fake", lambda t: (self.ok, self.detail))
        return [{"name": "svc", "type": "fake"}]


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setattr(watch, "CONFIRM", 2)
    p = _Probe()
    p.targets = p.install(monkeypatch)
    return p


def _pass(probe, state, when=None):
    return watch.run_pass(probe.targets, state, when or dt.datetime(2026, 9, 23, 12, 0))


def test_healthy_says_nothing(probe):
    state = {}
    msgs, down = _pass(probe, state)
    assert msgs == [] and down == []
    assert state["svc"]["ok"] is True


def test_one_blip_is_not_an_outage(probe):
    """CONFIRM=2, so a single failed pass must not page. This is the whole reason CONFIRM exists."""
    state = {}
    probe.ok = False
    msgs, down = _pass(probe, state)
    assert msgs == [] and down == []
    probe.ok = True
    msgs, _ = _pass(probe, state)
    assert msgs == []
    assert state["svc"]["fails"] == 0


def test_a_real_outage_is_reported_once(probe):
    state = {}
    probe.ok, probe.detail = False, "connection refused"
    _pass(probe, state)                       # fail 1 of 2, silent
    msgs, down = _pass(probe, state)          # fail 2 of 2, speaks
    assert len(msgs) == 1 and "svc" in msgs[0] and "connection refused" in msgs[0]
    assert down == ["svc"]
    for _ in range(5):                        # still down, still silent
        msgs, down = _pass(probe, state)
        assert msgs == [] and down == ["svc"]


def test_recovery_is_reported(probe):
    state = {}
    probe.ok = False
    _pass(probe, state)
    _pass(probe, state)
    probe.ok, probe.detail = True, "200"
    msgs, down = _pass(probe, state)
    assert len(msgs) == 1 and "is back" in msgs[0]
    assert down == []
    msgs, _ = _pass(probe, state)             # and then quiet again
    assert msgs == []


def test_the_alert_carries_the_hint(monkeypatch):
    monkeypatch.setattr(watch, "CONFIRM", 1)
    monkeypatch.setitem(watch.PROBES, "fake", lambda t: (False, "down"))
    targets = [{"name": "svc", "type": "fake", "hint": "check the zuliprc"}]
    msgs, _ = watch.run_pass(targets, {}, dt.datetime(2026, 9, 23, 12, 0))
    assert "check the zuliprc" in msgs[0]


def test_a_crashing_probe_does_not_take_the_pass_down(monkeypatch):
    """A watchdog that dies on one bad target stops watching the other sixteen."""
    monkeypatch.setattr(watch, "CONFIRM", 1)
    def boom(t):
        raise ValueError("bad config")
    monkeypatch.setitem(watch.PROBES, "boom", boom)
    monkeypatch.setitem(watch.PROBES, "fine", lambda t: (True, "ok"))
    targets = [{"name": "a", "type": "boom"}, {"name": "b", "type": "fine"}]
    msgs, down = watch.run_pass(targets, {}, dt.datetime(2026, 9, 23, 12, 0))
    assert down == ["a"] and "bad config" in msgs[0]


def test_an_unknown_probe_type_is_down_not_silent(monkeypatch):
    monkeypatch.setattr(watch, "CONFIRM", 1)
    msgs, down = watch.run_pass([{"name": "x", "type": "nope"}], {},
                                dt.datetime(2026, 9, 23, 12, 0))
    assert down == ["x"] and "unknown probe type" in msgs[0]


# ── heartbeat: the only probe that separates "running" from "working" ───────────────────────

def test_a_fresh_heartbeat_is_healthy(tmp_path):
    p = tmp_path / "relay"
    p.write_text("1")
    ok, detail = watch.probe_heartbeat({"path": str(p), "max_age_s": 300})
    assert ok is True and "ago" in detail


def test_a_stale_heartbeat_is_the_relay_outage(tmp_path):
    """The relay logged `Invalid API key` for hours while systemctl said active. Its port was
    fine and its unit was fine. Only the mtime would have shown it."""
    p = tmp_path / "relay"
    p.write_text("1")
    old = dt.datetime.now().timestamp() - 3600
    os.utime(p, (old, old))
    ok, detail = watch.probe_heartbeat({"path": str(p), "max_age_s": 300})
    assert ok is False and "3600s ago" in detail


def test_a_missing_heartbeat_is_down(tmp_path):
    ok, detail = watch.probe_heartbeat({"path": str(tmp_path / "never"), "max_age_s": 300})
    assert ok is False and "no heartbeat" in detail


# ── digest: so silence means healthy rather than dead ────────────────────────────────────────

def test_the_digest_fires_once_a_day_after_its_hour():
    state = {}
    early = dt.datetime(2026, 9, 23, 6, 0)
    assert watch.digest_due(state, early, hour=8) is False
    later = dt.datetime(2026, 9, 23, 9, 0)
    assert watch.digest_due(state, later, hour=8) is True
    state["_meta"] = {"digest_day": "2026-09-23"}
    assert watch.digest_due(state, later, hour=8) is False
    assert watch.digest_due(state, dt.datetime(2026, 9, 24, 9, 0), hour=8) is True


def test_the_digest_can_be_turned_off():
    assert watch.digest_due({}, dt.datetime(2026, 9, 23, 23, 0), hour=-1) is False


def test_the_digest_names_what_is_down():
    targets = [{"name": "a"}, {"name": "b"}]
    assert "all 2 services healthy" in watch.digest_text(targets, {}, [])
    assert "1 of 2 down — a" in watch.digest_text(targets, {}, ["a"])


# ── state and config ─────────────────────────────────────────────────────────────────────────

def test_state_survives_a_round_trip(tmp_path):
    path = str(tmp_path / "s" / "state.json")
    watch.save_state({"svc": {"ok": False, "fails": 3}}, path)
    assert watch.load_state(path)["svc"]["fails"] == 3


def test_missing_state_starts_clean(tmp_path):
    assert watch.load_state(str(tmp_path / "nope.json")) == {}


def test_the_shipped_targets_file_is_usable():
    """Every shipped target must name a probe that exists and carry its required keys, or the
    watchdog silently reports a target as down forever for a reason that is its own fault."""
    targets = watch.load_targets(os.path.join(os.path.dirname(watch.__file__), "targets.json"))
    assert len(targets) >= 10
    required = {"http": ["url"], "unit": ["unit"], "container": ["container"],
                "heartbeat": ["path"]}
    for t in targets:
        assert t["type"] in watch.PROBES, t
        for key in required[t["type"]]:
            assert t.get(key), t
        assert t.get("hint"), f"{t['name']} has no hint; an alert with no next step is noise"
    assert len({t["name"] for t in targets}) == len(targets), "duplicate target names"


def test_disabled_targets_are_skipped(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"targets": [{"name": "on", "type": "http", "url": "x"},
                                            {"name": "off", "type": "http", "url": "x",
                                             "enabled": False}]}))
    assert [t["name"] for t in watch.load_targets(str(path))] == ["on"]
