"""T2.1: morning briefing / Friday review — calendar+mail (via n8n) + tasks/gate (via the engine).

Read-only. The rule under test throughout: a section that failed to load must say so by name,
never render as an empty "nothing today".
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import render as R  # noqa: E402
import relay  # noqa: E402

OP, BOT = 10, 99

CAL_OK_EMPTY = {"mode": "morning", "generated_at": "2026-09-23T07:00:00-04:00",
                "calendar_ok": True, "calendar": [], "calendar_error": None,
                "mail_ok": True, "mail_needing_reply": [], "mail_error": None}

CAL_WITH_EVENT = {**CAL_OK_EMPTY, "calendar": [
    {"id": "e1", "summary": "IT Nomad Lab: weekly signal check", "start": "2026-09-23T09:00:00+02:00",
     "end": "2026-09-23T09:15:00+02:00", "all_day": False}]}

MAIL_BROKEN = {**CAL_OK_EMPTY, "mail_ok": False, "mail_needing_reply": None,
               "mail_error": "Client authentication failed (e.g., unknown client, no client "
                             "authentication included, or unsupported authentication method)."}


class FakeEngine:
    def __init__(self):
        self.runs = {}
        self.projects = []
        self.payload = CAL_OK_EMPTY
        self.briefing_error = None
        self.calls = []

    def add(self, rid, status, lane="comms", created_at=None, **detail):
        self.runs[rid] = {"run_id": rid, "lane": lane, "status": status,
                          "created_at": created_at or dt.datetime.now(dt.timezone.utc).isoformat(), **detail}

    def get(self, path, **p):
        if path == "/runs":
            return {"runs": list(self.runs.values())}
        if path == "/project-goals":
            return {"projects": self.projects}
        raise KeyError(path)

    def post(self, path, body, timeout=None):
        return {"status": "ok"}

    def briefing(self):
        return []

    def briefing_data(self, mode):
        self.calls.append(mode)
        if self.briefing_error:
            raise self.briefing_error
        return dict(self.payload, mode=mode)


class FakeChat:
    def __init__(self):
        self.dms, self.sent = [], []

    def send(self, channel, topic, content):
        self.sent.append((channel, topic, content))
        return len(self.sent)

    def dm(self, user_id, content):
        self.dms.append(content)
        return 0

    def humans(self):
        return [OP]


class Inline:
    def submit(self, fn, *a):
        fn(*a)


def make(tmp_path):
    eng, chat = FakeEngine(), FakeChat()
    r = relay.Relay(eng, chat, relay.State(str(tmp_path / "s.json")), OP, BOT)
    r.pool = Inline()
    return r, eng, chat


# ── render.py: pure section builders ────────────────────────────────────────────────────────────
def test_calendar_section_reports_failure_not_empty_day():
    broken = {"calendar_ok": False, "calendar_error": "token expired"}
    assert "did not load" in R.calendar_section(broken)
    assert "token expired" in R.calendar_section(broken)


def test_calendar_section_real_empty_reads_as_nothing_scheduled():
    text = R.calendar_section({"calendar_ok": True, "calendar": []})
    assert "did not load" not in text and "nothing in this window" in text


def test_calendar_section_lists_real_events():
    text = R.calendar_section(CAL_WITH_EVENT)
    assert "09:00" in text and "weekly signal check" in text


def test_calendar_section_converts_event_tz_to_digest_tz():
    # 09:00+02:00 is 03:00 in America/New_York (EDT, UTC-4) on this date — a naive string slice
    # would wrongly show 09:00 as if it were already Eastern.
    text = R.calendar_section(CAL_WITH_EVENT, tz=relay.TZ)
    assert "03:00" in text
    assert "09:00" not in text


def test_mail_section_reports_failure_not_empty_inbox():
    text = R.mail_section(MAIL_BROKEN)
    assert "did not load" in text and "Client authentication failed" in text


def test_mail_section_real_empty_reads_as_nothing_needing_reply():
    text = R.mail_section({"mail_ok": True, "mail_needing_reply": []})
    assert "did not load" not in text and "nothing unread" in text


def test_mail_section_lists_real_messages():
    text = R.mail_section({"mail_ok": True,
                           "mail_needing_reply": [{"from": "boss@corp.com", "subject": "re: budget"}]})
    assert "boss@" in text and "corp.com" in text and "re: budget" in text


def test_open_tasks_section_only_lists_incomplete_projects():
    text = R.open_tasks_section([{"title": "Done project", "done": 5, "total": 5},
                                 {"title": "Live project", "done": 2, "total": 5}])
    assert "Done project" not in text and "Live project: 3 open (2/5)" in text


def test_gate_queue_section_empty_vs_populated():
    assert "nothing waiting" in R.gate_queue_section([])
    text = R.gate_queue_section([{"run_id": "abc123", "lane": "comms", "proposal": {"preview": "send it"}}])
    assert "abc123" in text and "send it" in text


def test_moved_this_week_section_names_counts():
    text = R.moved_this_week_section([{"run_id": "e1", "lane": "research"}], failed_count=2)
    assert "1 executed, 2 failed" in text and "e1" in text


def test_briefing_message_title_by_mode():
    msg = R.briefing_message("friday", "now", ["a section"])
    assert "Friday review" in msg
    msg2 = R.briefing_message("morning", "now", ["a section"])
    assert "Morning briefing" in msg2


# ── relay.py: assembling + gating the scheduled post ────────────────────────────────────────────
def test_briefing_text_surfaces_real_empty_day_honestly(tmp_path):
    r, eng, chat = make(tmp_path)
    eng.payload = CAL_OK_EMPTY
    text = r.briefing_text("morning")
    assert "nothing in this window" in text and "nothing unread" in text
    assert "did not load" not in text


def test_briefing_text_never_masks_a_broken_upstream_as_empty(tmp_path):
    r, eng, chat = make(tmp_path)
    eng.payload = MAIL_BROKEN
    text = r.briefing_text("morning")
    assert "did not load" in text and "Client authentication failed" in text
    assert "nothing unread" not in text


def test_briefing_text_n8n_unreachable_is_a_named_failure(tmp_path):
    r, eng, chat = make(tmp_path)
    eng.briefing_error = ConnectionError("connection refused")
    text = r.briefing_text("morning")
    assert "n8n unreachable" in text
    assert "Could not reach the n8n briefing source" in text


def test_briefing_text_friday_adds_weekly_section(tmp_path):
    r, eng, chat = make(tmp_path)
    eng.add("done1", "executed", created_at=dt.datetime.now(dt.timezone.utc).isoformat())
    eng.add("old1", "executed", created_at="2020-01-01T00:00:00+00:00")   # outside the 7d window
    text = r.briefing_text("friday")
    assert "1 executed, 0 failed" in text
    assert "old1" not in text


def test_briefing_text_uses_mode_specific_calendar_window(tmp_path):
    r, eng, chat = make(tmp_path)
    r.briefing_text("morning")
    r.briefing_text("friday")
    assert eng.calls == ["morning", "friday"]


def test_dm_briefing_and_friday_review_commands(tmp_path):
    r, eng, chat = make(tmp_path)
    eng.payload = CAL_WITH_EVENT
    r.command("briefing")
    assert "weekly signal check" in chat.dms[-1] and "Morning briefing" in chat.dms[-1]
    r.command("friday review")
    assert "Friday review" in chat.dms[-1]


def test_scheduled_digest_stays_plain_summary_while_unarmed(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "BRIEFING_ARMED", False)
    r, eng, chat = make(tmp_path)
    at = dt.datetime(2026, 9, 25, 7, 40, tzinfo=relay.TZ)   # a Friday
    r.maybe_digest(at)
    assert "Digest 07:30" in chat.sent[-1][2]
    assert eng.calls == []                                  # briefing_data never called: unarmed


def test_scheduled_digest_uses_briefing_once_armed(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "BRIEFING_ARMED", True)
    r, eng, chat = make(tmp_path)
    morning = dt.datetime(2026, 9, 25, 7, 40, tzinfo=relay.TZ)
    r.maybe_digest(morning)
    assert "Morning briefing" in chat.sent[-1][2]
    assert eng.calls == ["morning"]


def test_scheduled_digest_friday_evening_gets_weekly_review(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "BRIEFING_ARMED", True)
    r, eng, chat = make(tmp_path)
    friday_evening = dt.datetime(2026, 9, 25, 19, 10, tzinfo=relay.TZ)   # 2026-09-25 is a Friday
    assert friday_evening.weekday() == 4
    r.maybe_digest(friday_evening)
    assert "Friday review" in chat.sent[-1][2]
    assert eng.calls == ["friday"]


def test_scheduled_digest_non_friday_evening_stays_plain_even_when_armed(tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "BRIEFING_ARMED", True)
    r, eng, chat = make(tmp_path)
    thursday_evening = dt.datetime(2026, 9, 24, 19, 10, tzinfo=relay.TZ)
    assert thursday_evening.weekday() == 3
    r.maybe_digest(thursday_evening)
    assert "Digest 19:00" in chat.sent[-1][2]
    assert eng.calls == []


def test_parse_command_recognizes_briefing_verbs():
    assert R.parse_command("briefing") == ("briefing", None)
    assert R.parse_command("Friday Review") == ("friday_review", None)
    assert R.parse_command("brief me")[0] == "chat"          # not an exact match: goes to chat
