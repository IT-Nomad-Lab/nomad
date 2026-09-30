"""Tests for the autopilot's state store: it must survive a restart and a crash mid-write (R7).

Run: python3 -m pytest autopilot/ -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import state as st  # noqa: E402


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "nomad-autopilot" / "state.json")


def test_a_fresh_store_has_the_prd_defaults(path):
    s = st.StateStore(path)
    assert s.config() == {"budget_mode": "cap", "daily_cap_usd": 500.0,
                          "per_project_cap": 3, "global_cap": 6}
    assert s.project("nomad")["state"] == "off"
    assert not os.path.exists(path)          # nothing written until something changes


def test_round_trip_survives_a_restart(path):
    s = st.StateStore(path)
    s.start("runematch")
    s.finish_pass("runematch", "running", "2 worker(s)", [{"task": 1}, {"task": 2}])
    s.start("nomad")
    s.stop("nomad", "kill")
    s.update_config({"budget_mode": "unlimited", "global_cap": 4})
    s.add_spend(1.25)

    r = st.StateStore(path)                  # a restart: a new store over the same file
    assert r.snapshot() == s.snapshot()
    rm = r.project("runematch")
    assert (rm["enabled"], rm["state"], rm["detail"]) == (True, "running", "2 worker(s)")
    assert [w["task"] for w in rm["workers"]] == [1, 2]
    assert rm["last_pass"] is not None
    assert r.project("nomad")["state"] == "off"
    assert r.config()["budget_mode"] == "unlimited" and r.config()["global_cap"] == 4
    assert r.spent_today() == 1.25
    assert r.enabled_projects() == ["runematch"]


def test_write_is_atomic_and_leaves_no_temp_files(path, monkeypatch):
    s = st.StateStore(path)
    s.start("a")
    before = open(path).read()

    # A crash between writing the temp file and the rename: the old file must be untouched and
    # the half-written temp file cleaned up.
    def boom(src, dst):
        raise OSError("disk went away")
    monkeypatch.setattr(st.os, "replace", boom)
    with pytest.raises(OSError):
        s.start("b")
    monkeypatch.undo()

    assert open(path).read() == before
    assert os.listdir(os.path.dirname(path)) == ["state.json"]
    assert json.loads(before)["projects"]["a"]["enabled"] is True


def test_a_corrupt_file_is_moved_aside_not_fatal(path):
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:
        f.write('{"projects": {"a": ')        # truncated
    s = st.StateStore(path)
    assert s.enabled_projects() == []
    aside = [n for n in os.listdir(os.path.dirname(path)) if ".corrupt-" in n]
    assert len(aside) == 1


def test_an_unknown_stored_state_loads_as_error(path):
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:
        json.dump({"projects": {"a": {"enabled": True, "state": "dancing"}}}, f)
    p = st.StateStore(path).project("a")
    assert p["state"] == "error" and "dancing" in p["detail"]


def test_start_and_stop_transitions(path):
    s = st.StateStore(path)
    assert s.start("a") is True
    assert s.project("a")["state"] == "idle"
    assert s.start("a") is False             # already on: no second start event
    s.finish_pass("a", "running", "1 worker(s)", [{"task": 7}])

    assert s.stop("a", "finish") is True
    p = s.project("a")
    assert (p["enabled"], p["state"]) == (False, "off")
    assert "finishing" in p["detail"]
    kinds = [e["kind"] for e in p["events"]]
    assert kinds.count("start") == 1 and kinds[-1] == "stop"

    with pytest.raises(ValueError):
        s.stop("a", "gently")


def test_a_pass_that_ends_after_stop_does_not_turn_the_project_back_on(path):
    s = st.StateStore(path)
    s.start("a")
    s.stop("a", "finish")
    s.finish_pass("a", "running", "1 worker(s)", [{"task": 1}])
    p = s.project("a")
    assert (p["enabled"], p["state"]) == (False, "off")
    assert p["workers"] == [{"task": 1}]     # the worker list is still tracked while it finishes


def test_event_log_is_capped_per_project(path):
    s = st.StateStore(path, max_events=5)
    for i in range(12):
        s.event("a", "note", f"n{i}")
    s.event("b", "note", "only one")
    assert [e["msg"] for e in s.project("a")["events"]] == [f"n{i}" for i in range(7, 12)]
    assert len(s.project("b")["events"]) == 1


@pytest.mark.parametrize("patch", [
    {"budget_mode": "yolo"}, {"daily_cap_usd": -1}, {"daily_cap_usd": "5"},
    {"per_project_cap": 2.5}, {"global_cap": True}, {"nonsense": 1},
])
def test_bad_config_is_refused_and_nothing_changes(path, patch):
    s = st.StateStore(path)
    with pytest.raises(ValueError):
        s.update_config(patch)
    assert s.config() == st.DEFAULT_CONFIG


def test_spend_rolls_over_at_midnight(path, monkeypatch):
    s = st.StateStore(path)
    s.add_spend(3)
    s._data["spend"]["date"] = "2000-01-01"  # as if the 3 dollars were yesterday's
    assert s.spent_today() == 0.0
