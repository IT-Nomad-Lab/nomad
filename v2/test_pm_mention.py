"""Mention a bot in OpenProject → an agent session (pm_mention.py, docs/prd/op-mention-agent.md).

Offline: a fake OpenProject and a fake dispatcher; jobs run inline (sync=True) unless a test is
about the threads.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pm_follow  # noqa: E402
import pm_mention  # noqa: E402
from pm_openproject import PMError  # noqa: E402
from test_pm_follow import FakeOP, act, wp  # noqa: E402

OPERATOR = 4
BOT_IDS = {"nomad-dev": 9, "nomad-research": 6, "nomad-manager": 10}   # others have no token


def tag(uid, name):
    return f'<mention class="mention" data-id="{uid}" data-type="user" data-text="@{name}">@{name}</mention>'


class OP(FakeOP):
    def __init__(self):
        super().__init__()
        self.boards = {}

    def user_id_of(self, login):
        return BOT_IDS.get(login)

    def board_of(self, w):
        return self.boards.get(w["id"], "nomad-work")


class Dispatcher:
    def __init__(self, project=None, session=None):
        self.calls = []
        self.project = project or {"project": "nomad", "session": {
            "session_id": "abcdef12-0000", "branch": "op/5-fix-it", "resumed": False}}
        self.session = session or {"ok": True, "session_id": "abcdef12-0000", "summary": "All done.",
                                   "worktree": "/home/x/nomad-wt/op-5", "branch": "op/5-fix-it",
                                   "diffstat": " a.py | 2 +-", "changed": [" M a.py", "?? b.py"],
                                   "secs": 75, "cost_usd": 0.4, "mode": "build"}

    def __call__(self, route, body, timeout):
        self.calls.append((route, body))
        return self.project if route == "/session/project" else self.session


def setup(dispatcher=None, **kw):
    op = OP()
    op.wps[5] = wp(5, "Fix it", status="In Progress")
    op.wps[5]["description"] = {"raw": "The importer crashes on empty rows."}
    op.acts[5] = [act(1, 6, comment="I looked at it; it is the null check."),
                  act(2, OPERATOR, comment="thanks")]
    d = dispatcher or Dispatcher()
    logs = []
    m = pm_mention.Mentions(op, d, OPERATOR, sync=True, log=lambda **e: logs.append(e), **kw)
    return op, d, m, logs


def comment(m, raw, user=OPERATOR, closed=False, wp_id=5, aid=50):
    a = act(aid, user, comment=raw)
    return m.consider(m.op.wps[wp_id], a, raw, user == OPERATOR, closed)


def test_an_operator_mention_runs_one_session_as_that_bot():
    op, d, m, logs = setup()
    r = comment(m, f"{tag(9, 'Dev')} fix the null check and add a test")
    assert r == {"bot": "nomad-dev", "state": "started"}
    assert [c[0] for c in d.calls] == ["/session/project", "/session"]
    proj = d.calls[0][1]
    assert proj == {"board": "nomad-work", "comment": "@Dev fix the null check and add a test",
                    "subject": "Fix it", "key": "op-5"}
    s = d.calls[1][1]
    assert s["key"] == "op-5" and s["project"] == "nomad" and s["bot"] == "nomad-dev"
    assert s["mode"] == "build" and s["title"] == "Fix it"
    # The ack, then the result, both as the bot.
    (w1, ack, as1), (w2, res, as2) = op.comments
    assert w1 == w2 == 5 and as1 == as2 == "nomad-dev"
    assert ack == "On it. Session `abcdef12`, project `nomad`, branch `op/5-fix-it`."
    assert res.startswith("**Done** · 1m 15s · $0.40") and "All done." in res
    assert "`~/nomad-wt/op-5`" in res and "```\na.py | 2 +-\n```" in res and "New files: `b.py`" in res
    assert logs[0]["run_id"] == "op-5" and logs[0]["outcome"] == "done"
    assert logs[0]["agent"] == "nomad-dev"


def test_the_prompt_carries_the_role_the_task_and_the_instruction():
    op, d, m, _ = setup()
    comment(m, f"{tag(6, 'Research')} why does it crash?")
    p = d.calls[1][1]["prompt"]
    assert p.startswith("You are Research, one of NOMAD's agents. You investigate")
    assert "## Work package #5: Fix it" in p and "Status: In Progress" in p
    assert "The importer crashes on empty rows." in p
    assert "- Research (agent): I looked at it; it is the null check." in p
    assert p.rstrip().endswith("## The operator's instruction\n@Research why does it crash?")


def test_comments_that_must_not_trigger_start_nothing():
    op, d, m, _ = setup()
    assert comment(m, f"{tag(9, 'Dev')} do it", user=9) is None          # a bot's comment
    assert comment(m, "no mention here") is None                         # no bot
    assert comment(m, f"{tag(OPERATOR, 'Josias')} note to self") is None  # only the operator
    # The Zulip prefix typed by an agent is not the operator.
    assert comment(m, "**Operator, via Zulip:**\n\n@Dev go", user=6) is None
    assert not d.calls and not op.comments


def test_a_zulip_reply_with_a_plain_at_name_triggers():
    op, d, m, _ = setup()
    r = comment(m, "**Operator, via Zulip:**\n\n@dev please retry", user=10)   # posted as manager
    assert r["bot"] == "nomad-dev"
    assert d.calls[1][1]["prompt"].endswith("@dev please retry")


def test_the_first_bot_mentioned_answers():
    _, d, m, _ = setup()
    assert comment(m, f"{tag(6, 'Research')} and {tag(9, 'Dev')} look")["bot"] == "nomad-research"


def test_plan_only_is_a_read_only_run():
    op, d, m, _ = setup()
    comment(m, f"{tag(9, 'Dev')} plan only: how would you fix it?")
    assert d.calls[1][1]["mode"] == "plan"
    assert op.comments[0][1].endswith(", read-only.")


def test_no_project_replies_with_the_choices_and_runs_nothing():
    d = Dispatcher(project={"error": "board content-creation has several projects; say which with a tag",
                            "choices": ["content-creation-app", "content-creation-backend"]})
    op, d, m, _ = setup(d)
    comment(m, f"{tag(9, 'Dev')} fix it")
    assert [c[0] for c in d.calls] == ["/session/project"]
    (_, text, who), = op.comments
    assert who == "nomad-dev" and "several projects" in text
    assert "`[content-creation-app]`, `[content-creation-backend]`" in text


def test_a_failed_run_posts_the_error():
    d = Dispatcher(session={"ok": False, "error": "claude exited 1", "worktree": "/home/x/w",
                            "branch": "op/5-x"})
    op, d, m, logs = setup(d)
    comment(m, f"{tag(9, 'Dev')} go")
    assert op.comments[-1][1].startswith("**Run failed** · Dev: claude exited 1")
    assert "`~/w`" in op.comments[-1][1] and logs[0]["outcome"] == "failed"


class LimitOnce(Dispatcher):
    """The first /session hits the usage limit, like the fake claude in dispatcher tests."""

    def __call__(self, route, body, timeout):
        self.calls.append((route, body))
        if route == "/session/project":
            return self.project
        if [c[0] for c in self.calls].count("/session") == 1:
            return {"ok": False, "limited": True, "reset_at": 4102444800, "reset_clock": "15:30",
                    "session_id": "abcdef12-0000", "error": "Claude AI usage limit reached|4102444800",
                    "worktree": "/home/x/nomad-wt/op-5", "branch": "op/5-fix-it"}
        return self.session


def test_a_usage_limit_hit_pauses_the_job_and_reruns_it_at_the_reset():
    op, d, m, logs = setup(LimitOnce())
    comment(m, f"{tag(9, 'Dev')} fix it")
    assert op.comments[-1] == (5, "Claude usage limit reached; resuming at 15:30", "nomad-dev")
    assert m.paused == {5: 4102444800} and 5 in m.active
    (job,) = m.pending[5]
    assert job["session_id"] == "abcdef12-0000" and job["limited"] is True
    assert logs[0]["outcome"] == "paused on the usage limit"
    # A later mention on the task waits behind the paused job.
    assert comment(m, f"{tag(9, 'Dev')} also this", aid=51)["state"] == "queued"
    assert [c[0] for c in d.calls].count("/session") == 1
    m.wake(5)                                              # the reset time
    runs = [b for r, b in d.calls if r == "/session"]
    assert len(runs) == 3 and runs[1]["key"] == "op-5"
    assert "stopped on the Claude usage limit" in runs[1]["prompt"]
    assert runs[2]["prompt"].endswith("@Dev also this")
    assert not m.paused and not m.active
    assert op.comments[-1][1].startswith("**Done**")


def test_a_paused_job_reruns_on_its_own_when_threads_run():
    resume_at = time.time() + 0.2

    class Soon(LimitOnce):
        def __call__(self, route, body, timeout):
            r = super().__call__(route, body, timeout)
            return {**r, "reset_at": resume_at} if r.get("limited") else r

    op = OP()
    op.wps[5] = wp(5, "Fix it", status="In Progress")
    d = Soon()
    m = pm_mention.Mentions(op, d, OPERATOR, cap=1)
    comment(m, f"{tag(9, 'Dev')} go")
    _until(lambda: [c[0] for c in d.calls].count("/session") == 2)
    _until(lambda: not m.active)
    assert any(t.startswith("Claude usage limit reached") for _, t, _ in op.comments)


def test_a_resumed_session_says_continuing():
    d = Dispatcher(project={"project": "nomad", "session": {"session_id": "abcdef12", "branch": "b",
                                                            "resumed": True}})
    op, d, m, _ = setup(d)
    comment(m, f"{tag(9, 'Dev')} and now?")
    assert op.comments[0][1].startswith("Continuing. Session `abcdef12`")


def test_a_closed_task_says_reopen_and_runs_nothing():
    op, d, m, _ = setup()
    comment(m, f"{tag(9, 'Dev')} go", closed=True)
    assert not d.calls and op.comments[0][1] == "This task is closed; reopen it to continue."


def test_an_autopilot_worker_on_the_task_blocks_the_run():
    op, d, m, _ = setup(busy=lambda wp_id: wp_id == 5)
    comment(m, f"{tag(9, 'Dev')} go")
    assert not d.calls and "autopilot has a worker" in op.comments[0][1]


def test_a_long_summary_is_trimmed():
    d = Dispatcher()
    d.session = {**d.session, "summary": "x" * 9000}
    op, d, m, _ = setup(d)
    comment(m, f"{tag(9, 'Dev')} go")
    assert "(trimmed; the full text is in the engine log)" in op.comments[-1][1]


def test_an_unknown_mention_refreshes_the_bot_map_once(monkeypatch):
    op, d, m, _ = setup()
    m.bot_ids()
    BOT_IDS["nomad-ads"] = 12
    try:
        m._ids_at = 0
        assert comment(m, f"{tag(12, 'Ads')} write the ad")["bot"] == "nomad-ads"
    finally:
        BOT_IDS.pop("nomad-ads")


def test_the_follow_module_hands_operator_comments_over_and_records_it():
    op, d, m, _ = setup()
    f = pm_follow.Follow(op, operator_id=OPERATOR, mentions=m)
    raw = f"{tag(9, 'Dev')} go"
    a = act(60, OPERATOR, comment=raw)
    a["_links"]["workPackage"]["href"] = "/api/v3/work_packages/5"
    ev = f.ingest({"action": "work_package_comment:comment", "activity": a})
    assert ev["session"] == {"bot": "nomad-dev", "state": "started"}
    # The same activity again (a webhook repeat) starts nothing.
    assert f.ingest({"action": "work_package_comment:comment", "activity": a}) is None
    assert [c[0] for c in d.calls].count("/session") == 1


def test_one_run_per_task_and_a_cap_across_tasks():
    """Threads for real: a second mention on a busy task queues behind it; a third task waits for
    a free worker when the cap is reached."""
    gate = threading.Event()
    started = []

    class Slow(Dispatcher):
        def __call__(self, route, body, timeout):
            if route == "/session":
                started.append(body["key"])
                gate.wait(5)
            return super().__call__(route, body, timeout)

    op = OP()
    for i in (5, 6, 7):
        op.wps[i] = wp(i, f"Task {i}", status="In Progress")
    d = Slow()
    m = pm_mention.Mentions(op, d, OPERATOR, cap=2)
    st = [comment(m, f"{tag(9, 'Dev')} a", wp_id=5, aid=1)["state"]]
    _until(lambda: "op-5" in started)
    st.append(comment(m, f"{tag(9, 'Dev')} b", wp_id=5, aid=2)["state"])
    st.append(comment(m, f"{tag(9, 'Dev')} c", wp_id=6, aid=3)["state"])
    _until(lambda: "op-6" in started)
    st.append(comment(m, f"{tag(9, 'Dev')} d", wp_id=7, aid=4)["state"])
    assert st == ["started", "queued", "started", "waiting"]
    time.sleep(0.1)
    assert sorted(started) == ["op-5", "op-6"]            # the cap holds; one per task
    gate.set()
    _until(lambda: len(started) == 4)
    assert started.count("op-5") == 2 and "op-7" in started
    _until(lambda: not m.active)
    notes = [t for _, t, _ in op.comments if t.startswith(("Queued", "Waiting"))]
    assert len(notes) == 2


def _until(cond, secs=5):
    end = time.time() + secs
    while not cond():
        if time.time() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_a_bot_without_a_token_is_not_a_bot():
    class Bare(OP):
        def user_id_of(self, login):
            if login == "nomad-dev":
                raise PMError("unreachable")
            return None
    m = pm_mention.Mentions(Bare(), Dispatcher(), OPERATOR, sync=True)
    assert m.bot_ids() == {}


def test_closing_a_task_drops_a_clean_worktree_quietly():
    d = Dispatcher()
    d.session = {"ok": True, "known": True, "removed": True, "worktree": "/home/x/w", "bot": "nomad-dev"}
    op, d, m, _ = setup(d)
    f = pm_follow.Follow(op, operator_id=OPERATOR, mentions=m)
    op.wps[5]["_links"]["status"]["title"] = "Done"
    op.acts[5] = [act(70, 9, details=["Status changed from In Progress to Done"])]
    f.ingest({"action": "work_package:updated", "work_package": {"id": 5}})
    assert d.calls == [("/session/close", {"key": "op-5"})] and not op.comments


def test_closing_a_task_with_changes_keeps_the_worktree_and_says_so():
    d = Dispatcher()
    d.session = {"ok": True, "known": True, "removed": False, "worktree": "/home/x/nomad-wt/op-5",
                 "branch": "op/5-x", "changed": [" M a.py"], "bot": "nomad-research"}
    op, d, m, _ = setup(d)
    m.task_closed(5)
    (_, text, who), = op.comments
    assert who == "nomad-research" and "`~/nomad-wt/op-5`" in text and "1 uncommitted" in text


def test_a_status_change_between_closed_states_or_to_open_closes_nothing():
    op, d, m, _ = setup()
    f = pm_follow.Follow(op, operator_id=OPERATOR, mentions=m)
    op.acts[5] = [act(71, 9, details=["Status changed from Ready to In Progress"])]
    f.ingest({"action": "work_package:updated", "work_package": {"id": 5}})
    op.wps[5]["_links"]["status"]["title"] = "Won't do"
    op.acts[5].append(act(72, 9, details=["Status changed from Done to Won't do"]))
    f.ingest({"action": "work_package:updated", "work_package": {"id": 5}})
    assert not d.calls
