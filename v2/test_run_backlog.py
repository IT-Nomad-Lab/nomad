"""V2b · the self-development sweep the daily cron fires at.

`nomadselfdevcron` was pointing at crew's `/run-backlog`, which ran the work itself and left
uncommitted edits behind with nothing in front of it. The v2 sweep starts runs and they stop at the
gate, so a 07:00 cron queues proposals instead of acting while nobody is awake.

What these pin down is the restraint. A scheduler is on the other end, so the limit is clamped, one
project cannot starve the others, and one broken goal cannot stop the sweep.

Run: python3 -m pytest v2/test_run_backlog.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine as engine_mod  # noqa: E402


class Eng(engine_mod.Engine):
    """Real run_backlog over stubbed goals and task starts — nothing touches NocoDB."""

    def __init__(self, goals, fail=(), empty=()):
        self._goals, self._fail, self._empty = goals, set(fail), set(empty)
        self.calls = []

    def project_goals(self, limit=60):
        return self._goals

    def run_next_task(self, goal_id):
        self.calls.append(goal_id)
        if goal_id in self._fail:
            raise RuntimeError("nocodb unreachable")
        if goal_id in self._empty:
            return {"done": True, "note": "no backlog tasks remaining"}
        return {"task_id": 100 + goal_id, "task_title": f"task for {goal_id}",
                "run_id": f"run{goal_id}", "status": "awaiting-approval"}


def goals(*specs):
    """(goal_id, done, total) → the shape project_goals returns."""
    return [{"goal_id": g, "title": f"P{g}", "milestones": 1, "done": d, "total": t,
             "pct": round(100 * d / t) if t else 0} for g, d, t in specs]


def test_it_takes_one_task_from_each_project_not_all_from_the_first():
    """A long project must not starve the rest of the board."""
    e = Eng(goals((3, 0, 9), (2, 0, 9), (1, 0, 9)))
    r = e.run_backlog(limit=3)
    assert e.calls == [3, 2, 1]
    assert r["started"] == 3
    assert [x["goal_id"] for x in r["runs"]] == [3, 2, 1]


def test_the_limit_is_honored():
    e = Eng(goals((3, 0, 9), (2, 0, 9), (1, 0, 9)))
    assert e.run_backlog(limit=2)["started"] == 2
    assert e.calls == [3, 2]


def test_the_limit_is_clamped_because_a_scheduler_is_on_the_other_end():
    """A bad payload must not fill the gate faster than an operator can read it."""
    e = Eng(goals(*[(i, 0, 9) for i in range(20, 0, -1)]))
    r = e.run_backlog(limit=999)
    assert r["limit"] == engine_mod.Engine.BACKLOG_MAX
    assert r["started"] == engine_mod.Engine.BACKLOG_MAX


def test_a_nonsense_limit_still_runs_one():
    for bad in (0, -4, None):
        e = Eng(goals((1, 0, 9), (2, 0, 9)))
        assert e.run_backlog(limit=bad)["started"] == 1


def test_a_finished_project_is_never_asked():
    """Skipped before the call, so a completed board costs no lookups at all."""
    e = Eng(goals((3, 9, 9), (2, 0, 9)))
    e.run_backlog(limit=3)
    assert e.calls == [2]


def test_a_project_with_nothing_queued_does_not_count_against_the_limit():
    """`done < total` can still mean no Backlog rows: the remainder may be In Progress."""
    e = Eng(goals((3, 1, 9), (2, 1, 9), (1, 1, 9)), empty={3})
    r = e.run_backlog(limit=2)
    assert e.calls == [3, 2, 1]
    assert [x["goal_id"] for x in r["runs"]] == [2, 1]


def test_one_broken_project_does_not_stop_the_sweep():
    e = Eng(goals((3, 0, 9), (2, 0, 9)), fail={3})
    r = e.run_backlog(limit=2)
    assert [x["goal_id"] for x in r["runs"]] == [2]
    assert r["skipped"] == [{"goal_id": 3, "error": "nocodb unreachable"}]


def test_an_empty_board_starts_nothing_and_does_not_raise():
    r = Eng([]).run_backlog(limit=3)
    assert r["started"] == 0 and r["runs"] == [] and r["skipped"] == []


def test_the_reply_says_nothing_has_executed():
    """The cron's own log is the only place anyone sees this. It should not read as work done."""
    r = Eng(goals((1, 0, 9))).run_backlog(limit=1)
    assert "awaiting approval" in r["note"] and "executed" in r["note"]
    assert all(x["status"] == "awaiting-approval" for x in r["runs"])
