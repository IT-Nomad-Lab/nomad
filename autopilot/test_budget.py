"""Tests for the budget switch (PRD 5.9, R9, AC6; #880).

Run: python3 -m pytest autopilot/test_budget.py -q
"""
import datetime as dt
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import budget as bg  # noqa: E402
import state as st  # noqa: E402


class FakeDate(dt.date):
    """state.py rolls spend over on date.today(); tests move the clock through this."""
    current = dt.date(2026, 9, 28)

    @classmethod
    def today(cls):
        return cls.current


@pytest.fixture
def clock(monkeypatch):
    FakeDate.current = dt.date(2026, 9, 28)
    monkeypatch.setattr(st, "date", FakeDate)
    return FakeDate


@pytest.fixture
def store(tmp_path):
    return st.StateStore(str(tmp_path / "nomad-autopilot" / "state.json"))


@pytest.fixture
def budget(store, tmp_path):
    return bg.Budget(store, runs_dir=str(tmp_path / "runs"))


def write_run(tmp_path, project, name, cost, finished=True, mtime=None):
    d = tmp_path / "runs" / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.jsonl"
    lines = [{"type": "system", "subtype": "init"},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}]
    if finished:
        lines.append({"type": "result", "subtype": "success", "is_error": False,
                      "result": "autopilot: result ready to merge", "total_cost_usd": cost})
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return str(p)


# ---- defaults and the switch ----------------------------------------------------------------

def test_default_is_a_500_dollar_daily_cap(budget):
    assert budget.mode() == "cap" and budget.cap() == 500.0
    d = budget.check()
    assert d.allowed and d.remaining_usd == 500.0 and d.detail == ""


def test_mode_switch_is_persisted_across_a_restart(store, budget, tmp_path):
    budget.set_mode("unlimited")
    again = bg.Budget(st.StateStore(store.path), runs_dir=str(tmp_path / "runs"))
    assert again.mode() == "unlimited"
    again.set_mode("cap")
    assert bg.Budget(st.StateStore(store.path)).mode() == "cap"


def test_bad_mode_and_cap_are_refused(budget):
    with pytest.raises(ValueError):
        budget.set_mode("free")
    with pytest.raises(ValueError):
        budget.set_cap(-1)
    assert budget.mode() == "cap" and budget.cap() == 500.0


# ---- spend from the run logs ----------------------------------------------------------------

def test_run_cost_reads_the_result_event(tmp_path):
    assert bg.run_cost(write_run(tmp_path, "nomad", "880-1", 1.75)) == 1.75
    assert bg.run_cost(write_run(tmp_path, "nomad", "880-2", 0, finished=False)) is None
    assert bg.run_cost(str(tmp_path / "missing.jsonl")) is None


def test_run_cost_skips_junk_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('not json\n{"type": "result", "total_cost_usd": "oops"\n'
                 '{"type": "result", "total_cost_usd": 2.5}\n', encoding="utf-8")
    assert bg.run_cost(str(p)) == 2.5


def test_record_adds_cost_per_project_and_never_twice(budget, store, tmp_path):
    log = write_run(tmp_path, "nomad", "880-1", 3.0)
    fin = {"task": 880, "outcome": "merge", "cost_usd": 3.0, "log": log}
    assert budget.record("nomad", fin) == 3.0
    assert budget.record("nomad", fin) == 0.0                      # same run collected twice
    assert budget.record("runematch", {"task": 1, "cost_usd": 1.5}) == 1.5
    assert budget.spent_today() == 4.5
    assert store.spent_today() == 4.5                              # the store's figure agrees
    assert budget.status()["by_project"] == {"nomad": 3.0, "runematch": 1.5}


def test_record_parses_the_log_when_the_dict_has_no_cost(budget, tmp_path):
    log = write_run(tmp_path, "nomad", "880-1", 2.25)
    assert budget.record("nomad", {"task": 880, "log": log}) == 2.25


def test_record_log_ignores_a_run_still_going(budget, tmp_path):
    assert budget.record_log("nomad", write_run(tmp_path, "nomad", "a", 0, finished=False)) == 0.0
    assert budget.record_log("nomad", write_run(tmp_path, "nomad", "b", 4.0)) == 4.0
    assert budget.spent_today() == 4.0


def test_reconcile_counts_todays_uncounted_runs_only(budget, tmp_path):
    counted = write_run(tmp_path, "nomad", "880-1", 3.0)
    budget.record("nomad", {"task": 880, "cost_usd": 3.0, "log": counted})
    write_run(tmp_path, "nomad", "881-1", 2.0)                      # finished, never collected
    write_run(tmp_path, "runematch", "833-1", 1.0)
    write_run(tmp_path, "runematch", "834-1", 0, finished=False)    # still running
    write_run(tmp_path, "nomad", "old", 9.0, mtime=time.time() - 3 * 86400)   # another day
    assert budget.reconcile() == 3.0
    assert budget.reconcile() == 0.0                                # idempotent
    assert budget.spent_today() == 6.0
    assert budget.status()["by_project"] == {"nomad": 5.0, "runematch": 1.0}


def test_reconcile_with_no_runs_dir(budget):
    assert budget.reconcile() == 0.0


def test_spend_survives_a_restart(store, budget, tmp_path):
    budget.record("nomad", {"task": 1, "cost_usd": 2.0, "log": write_run(tmp_path, "nomad", "1", 2.0)})
    again = bg.Budget(st.StateStore(store.path), runs_dir=str(tmp_path / "runs"))
    assert again.spent_today() == 2.0
    assert again.reconcile() == 0.0                                 # still knows it was counted


# ---- the cap, the pause and midnight ---------------------------------------------------------

def test_cap_blocks_new_starts_once_reached(budget):
    budget.set_cap(5)
    budget.record("nomad", {"task": 1, "cost_usd": 4.99})
    assert budget.check().allowed
    budget.record("nomad", {"task": 2, "cost_usd": 0.01})
    d = budget.check()
    assert not d.allowed and d.remaining_usd == 0.0
    assert "daily cap $5.00 reached" in d.detail and "running workers finish" in d.detail


def test_cap_below_one_run_pauses_after_that_run(budget):
    """AC6: a cap below one run's cost. The run already going finishes; the next start waits."""
    budget.set_cap(0.5)
    assert budget.check().allowed                  # nothing spent yet: the first run may start
    budget.record("nomad", {"task": 1, "cost_usd": 2.0})
    assert not budget.check().allowed


def test_unlimited_resumes_and_cap_pauses_again(budget):
    budget.set_cap(1)
    budget.record("nomad", {"task": 1, "cost_usd": 3.0})
    assert not budget.check().allowed
    budget.set_mode("unlimited")
    d = budget.check()
    assert d.allowed and d.remaining_usd is None
    budget.set_mode("cap")
    assert not budget.check().allowed


def test_raising_the_cap_resumes(budget):
    budget.set_cap(1)
    budget.record("nomad", {"task": 1, "cost_usd": 3.0})
    assert not budget.check().allowed
    budget.set_cap(10)
    assert budget.check().allowed


def test_one_alert_per_project_per_pause(budget):
    budget.set_cap(1)
    budget.record("nomad", {"task": 1, "cost_usd": 3.0})
    assert not budget.check().allowed
    assert budget.mark_paused("nomad") is True
    assert budget.mark_paused("nomad") is False                   # the next pass: no new alert
    assert budget.mark_paused("runematch") is True
    assert budget.status()["paused_projects"] == ["nomad", "runematch"]

    budget.set_mode("unlimited")
    assert budget.check().allowed                                  # episode over
    budget.set_mode("cap")
    assert not budget.check().allowed
    assert budget.mark_paused("nomad") is True                     # a new pause alerts again


def test_midnight_resets_spend_and_pause(budget, clock):
    budget.set_cap(1)
    budget.record("nomad", {"task": 1, "cost_usd": 3.0})
    assert not budget.check().allowed
    budget.mark_paused("nomad")

    clock.current = dt.date(2026, 9, 29)
    d = budget.check()
    assert d.allowed and d.spent_usd == 0.0
    s = budget.status()
    assert s["date"] == "2026-09-29" and s["by_project"] == {} and s["paused_projects"] == []
    assert s["resets_at"] == dt.datetime(2026, 9, 30).timestamp()
    assert budget.mark_paused("nomad") is True


def test_counted_runs_reset_at_midnight(budget, clock, tmp_path):
    log = write_run(tmp_path, "nomad", "1", 2.0)
    budget.record("nomad", {"task": 1, "cost_usd": 2.0, "log": log})
    clock.current = dt.date(2026, 9, 29)
    assert budget.status()["spent_usd"] == 0.0
    assert "counted" not in budget.store.spend_record()


def test_status_shape(budget):
    s = budget.status()
    assert set(s) == {"mode", "cap_usd", "spent_usd", "remaining_usd", "allowed", "detail",
                      "date", "by_project", "paused_projects", "resets_at"}
    assert s["mode"] == "cap" and s["cap_usd"] == 500.0 and s["allowed"]


def test_default_runs_dir_sits_next_to_the_state_file(store, monkeypatch):
    monkeypatch.delenv("NOMAD_AUTOPILOT_RUNS", raising=False)
    assert bg.Budget(store).runs_dir == os.path.join(os.path.dirname(store.path), "runs")
