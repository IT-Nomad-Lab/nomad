"""Tests for the daily digest (PRD 5.7, R8, 9.3; #879): once per project per day, after 08:00,
only with something done or waiting, never twice.

Every channel is a fake: nothing here reaches Zulip or n8n.
Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest autopilot -q
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alerts as al  # noqa: E402
import digest as dg  # noqa: E402
import state as st  # noqa: E402
import supervisor as sv  # noqa: E402
import tasks as tk  # noqa: E402


class FakeChannel(al.Channel):
    def __init__(self, name, fail=0):
        self.name, self.fail, self.sent = name, fail, []

    def send(self, project, title, body):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("down")
        self.sent.append((project, title, body))


class Clock:
    def __init__(self, when):
        self.when = when

    def __call__(self):
        return self.when


class Sched:
    def __init__(self):
        self.selections = {}


def task(i, subject, status="Ready", lane="dev", categories=()):
    return tk.Task(id=i, subject=subject, status=status, type="Task", lane=lane,
                   categories=categories)


def board():
    """A selection with one of each kind the digest reports."""
    sk = [
        tk.Skipped(task(11, "Approve the store listing", "Awaiting approval"), "operator",
                   "needs operator: Awaiting approval"),
        tk.Skipped(task(12, "Flaky save test"), "operator",
                   "last run failed: tests red; press Run again to retry it"),
        tk.Skipped(task(13, "Leaderboard", "New"), "waiting", "blocked_by #11 (Awaiting approval)"),
        tk.Skipped(task(14, "Sign the contract", lane="manager"), "person", "person-only: manager lane"),
        tk.Skipped(task(15, "Record the trailer [human]"), "person", "person-only: tagged human"),
        tk.Skipped(task(16, "Pause menu"), "running", "held by a live worker"),
        tk.Skipped(task(17, "Combo meter"), "running", "in the merge queue"),
    ]
    return tk.Selection(qualified=[], skipped=sk)


@pytest.fixture
def env(tmp_path):
    store = st.StateStore(str(tmp_path / "state.json"))
    store.set_autopilot("rm", True)
    store.start("rm")
    store.finish_pass("rm", "running", "1 worker(s): #16", [{"task": 16}])
    zulip, email = FakeChannel("zulip"), FakeChannel("email")
    alerter = al.Alerter([zulip, email], al.Ledger(str(tmp_path / "alerts.json")))
    sched = Sched()
    sched.selections["rm"] = board()
    clock = Clock(datetime(2026, 9, 28, 7, 30))
    d = dg.Digest(store, alerter, scheduler=sched, runs_dir=str(tmp_path / "runs"), now=clock)
    return {"store": store, "zulip": zulip, "email": email, "alerter": alerter, "sched": sched,
            "clock": clock, "digest": d, "tmp": tmp_path}


def merge_event(store, tid, sha="0123456789abcdef"):
    store.event("rm", "merge", f"task {tid}: merged {sha} -> Done. autopilot: merged `ap-{tid}`")


def run_log(tmp, pid, name, usd, when):
    d = tmp / "runs" / pid
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps({"type": "result", "total_cost_usd": usd}) + "\n")
    os.utime(p, (when.timestamp(), when.timestamp()))


# ---- when it goes out -------------------------------------------------------------------------

def test_nothing_before_eight_but_merges_are_collected(env):
    merge_event(env["store"], 9)
    assert env["digest"].tick() == {}
    assert env["zulip"].sent == [] and env["email"].sent == []
    mark = json.loads((env["tmp"] / "digest.json").read_text())["projects"]["rm"]
    assert [m["task"] for m in mark["merged"]] == ["9"]


def test_one_digest_after_eight_and_never_twice(env):
    merge_event(env["store"], 9)
    env["clock"].when = datetime(2026, 9, 28, 8, 0, 30)
    out = env["digest"].tick()
    assert out["rm"] == {"zulip": "sent", "email": "sent"}
    for _ in range(3):
        env["clock"].when += timedelta(minutes=1)
        assert env["digest"].tick() == {}
    assert len(env["zulip"].sent) == 1 and len(env["email"].sent) == 1


def test_lost_marker_still_cannot_send_twice(env):
    """The ledger key digest:<pid>:<date> is the second guard (the trial posted one twice)."""
    env["clock"].when = datetime(2026, 9, 28, 9, 0)
    env["digest"].tick()
    os.remove(env["tmp"] / "digest.json")
    out = env["digest"].tick()
    assert out["rm"] == {"zulip": "dup", "email": "dup"}
    assert len(env["zulip"].sent) == 1


def test_started_after_eight_sends_on_the_first_pass(env):
    env["clock"].when = datetime(2026, 9, 28, 15, 12)
    assert env["digest"].tick()["rm"]["zulip"] == "sent"


def test_failed_channel_retried_without_resending_the_others(env):
    env["email"].fail = 1
    env["clock"].when = datetime(2026, 9, 28, 8, 1)
    assert env["digest"].tick()["rm"] == {"zulip": "sent", "email": "failed"}
    env["clock"].when += timedelta(minutes=1)
    assert env["digest"].tick()["rm"] == {"zulip": "dup", "email": "sent"}
    env["clock"].when += timedelta(minutes=1)
    assert env["digest"].tick() == {}
    assert len(env["zulip"].sent) == 1 and len(env["email"].sent) == 1


def test_nothing_done_or_waiting_sends_nothing(env):
    store = env["store"]
    store.finish_pass("rm", "idle", "nothing to do", [])
    env["sched"].selections["rm"] = tk.Selection([], [])
    env["clock"].when = datetime(2026, 9, 28, 8, 5)
    assert env["digest"].tick() == {"rm": "empty"}
    assert env["zulip"].sent == []
    env["clock"].when += timedelta(hours=1)
    assert env["digest"].tick() == {}          # marked for the day, not rebuilt every pass


def test_disabled_project_with_nothing_is_left_out(env):
    env["store"].stop("rm", "finish")
    env["store"].set_workers("rm", [])
    env["clock"].when = datetime(2026, 9, 28, 8, 5)
    assert env["digest"].tick() == {}


# ---- what it says -----------------------------------------------------------------------------

def test_body_covers_every_section(env):
    t = env["tmp"]
    merge_event(env["store"], 9, "abcdef1234567890")
    run_log(t, "rm", "a.jsonl", 3.5, datetime(2026, 9, 27, 14, 0))
    run_log(t, "rm", "b.jsonl", 1.25, datetime(2026, 9, 27, 23, 50))
    run_log(t, "other", "c.jsonl", 10.0, datetime(2026, 9, 27, 9, 0))
    run_log(t, "rm", "today.jsonl", 99.0, datetime(2026, 9, 28, 1, 0))       # not yesterday
    run_log(t, "rm", "old.jsonl", 99.0, datetime(2026, 9, 26, 12, 0))        # not yesterday
    env["clock"].when = datetime(2026, 9, 28, 8, 0)
    env["digest"].tick()
    project, title, body = env["zulip"].sent[0]
    assert project == "rm"
    assert title == ("rm daily digest 2026-09-28: 1 merged, 2 running, 2 need you, 1 blocked, "
                     "2 person-only")
    assert "#9 `abcdef1234`" in body
    assert "#16 Pause menu (worker)" in body and "#17 Combo meter (merge queue)" in body
    assert "#11 Approve the store listing: needs operator: Awaiting approval" in body
    assert "#12 Flaky save test: last run failed: tests red" in body
    assert "#13 Leaderboard: blocked_by #11 (Awaiting approval)" in body
    assert "#14 Sign the contract: manager lane" in body
    assert "#15 Record the trailer [human]: tagged human" in body
    assert "**Spend 2026-09-27:** $4.75 for rm, $14.75 across all projects" in body


def test_project_state_needing_you_is_listed(env):
    env["store"].finish_pass("rm", "budget_paused", "daily cap $500.00 reached", [])
    env["sched"].selections["rm"] = tk.Selection([], [])
    env["clock"].when = datetime(2026, 9, 28, 8, 0)
    env["digest"].tick()
    _, title, body = env["zulip"].sent[0]
    assert "1 need you" in title
    assert "- project is budget paused: daily cap $500.00 reached" in body


def test_next_day_covers_only_what_merged_since(env):
    store, clock, d = env["store"], env["clock"], env["digest"]
    merge_event(store, 9)
    clock.when = datetime(2026, 9, 28, 8, 0)
    d.tick()
    time.sleep(0.01)
    merge_event(store, 21)
    clock.when = datetime(2026, 9, 29, 8, 0)
    d.tick()
    body = env["zulip"].sent[1][2]
    assert "#21" in body and "#9 " not in body and "#9\n" not in body
    assert "Merged since the last digest (2026-09-28 08:00)" in body
    assert env["zulip"].sent[1][1].startswith("rm daily digest 2026-09-29: 1 merged")


def test_merges_survive_the_capped_event_log(tmp_path):
    """The store keeps a few events; tick() runs every pass, so none is lost by 08:00."""
    store = st.StateStore(str(tmp_path / "state.json"), max_events=3)
    store.set_autopilot("rm", True)
    store.start("rm")
    ch = FakeChannel("zulip")
    clock = Clock(datetime(2026, 9, 28, 1, 0))
    d = dg.Digest(store, al.Alerter([ch], al.Ledger(str(tmp_path / "a.json"))),
                  runs_dir=str(tmp_path / "runs"), now=clock)
    for i in range(10):
        store.event("rm", "merge", f"task {100 + i}: merged {'ab' * 5} -> Done.")
        store.event("rm", "state", "running")
        store.event("rm", "state", "idle")
        d.tick()
    clock.when = datetime(2026, 9, 28, 8, 0)
    d.tick()
    body = ch.sent[0][2]
    assert all(f"#{100 + i}" in body for i in range(10))
    assert "_The board was not read" in body          # no scheduler selection for rm


def test_merge_that_failed_is_not_counted_as_merged(env):
    env["store"].event("rm", "merge", "task 5: blocked -> Blocked. autopilot: merge blocked")
    env["store"].event("rm", "merge", "task 6: gate running on ap-6 (attempt 1)")
    env["clock"].when = datetime(2026, 9, 28, 8, 0)
    env["digest"].tick()
    assert env["zulip"].sent[0][1].startswith("rm daily digest 2026-09-28: 0 merged")


def test_tick_never_raises(env):
    class Boom:
        def notify(self, *a, **k):
            raise RuntimeError("boom")
    env["digest"].alerter = Boom()
    env["clock"].when = datetime(2026, 9, 28, 8, 0)
    assert env["digest"].tick() == {}


def test_parse_at():
    assert dg.parse_at("08:00").hour == 8
    assert dg.parse_at("7:30").minute == 30


# ---- the supervisor hook ----------------------------------------------------------------------

class CountingDigest:
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def tick(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("digest broke")


def test_supervisor_ticks_the_digest_every_pass(tmp_path):
    store = st.StateStore(str(tmp_path / "state.json"))
    store.set_autopilot("rm", True)
    store.start("rm")
    dig = CountingDigest()
    sup = sv.Supervisor(store, digest=dig)
    sup.run_all()
    sup.run_all()
    assert dig.calls == 2 and sup.passes == 2


def test_a_broken_digest_does_not_stop_the_pass(tmp_path):
    store = st.StateStore(str(tmp_path / "state.json"))
    store.set_autopilot("rm", True)
    store.start("rm")
    sup = sv.Supervisor(store, digest=CountingDigest(fail=True))
    sup.run_all()
    assert sup.passes == 1 and store.project("rm")["state"] == "idle"


def test_supervisor_has_no_digest_by_default(tmp_path):
    assert sv.Supervisor(st.StateStore(str(tmp_path / "s.json"))).digest is None
