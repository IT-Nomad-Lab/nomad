"""Tests for usage_limited (#960): the Max plan's limit message read the same way everywhere.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot -q
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from usage_limit import DEFAULT_WAIT, usage_limited  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover - no tz database
    PARIS = None

NOW = datetime(2026, 9, 30, 10, 0).timestamp()          # local time, like the messages


def test_the_epoch_form_gives_that_reset():
    reset = int(NOW) + 7200
    assert usage_limited(f"Claude AI usage limit reached|{reset}", "", now=NOW) == float(reset)


def test_a_clock_time_is_the_next_time_the_clock_shows_it():
    at = usage_limited("5-hour limit reached ∙ resets 3:30pm", "", now=NOW)
    assert at == datetime(2026, 9, 30, 15, 30).timestamp()
    at = usage_limited("You've hit your limit · resets 9am", "", now=NOW)
    assert at == datetime(2026, 10, 1, 9, 0).timestamp()            # 9am has passed: tomorrow


def test_a_named_time_zone_is_used():
    if PARIS is None:
        return
    now = datetime(2026, 9, 30, 13, 0, tzinfo=PARIS).timestamp()
    text = "Claude usage limit reached. Your limit will reset at 3pm (Europe/Paris)."
    assert usage_limited(text, None, now=now) == datetime(2026, 9, 30, 15, 0, tzinfo=PARIS).timestamp()


def test_a_dated_reset():
    at = usage_limited("Weekly limit reached ∙ resets Oct 3, 9am", "", now=NOW)
    assert at == datetime(2026, 10, 3, 9, 0).timestamp()


def test_no_reset_time_means_now_plus_five_hours():
    assert DEFAULT_WAIT == 5 * 3600
    assert usage_limited("Claude AI usage limit reached", "", now=NOW) == NOW + DEFAULT_WAIT
    # a reset already in the past reads as none
    assert usage_limited("Claude AI usage limit reached|1000", "", now=NOW) == NOW + DEFAULT_WAIT


def test_the_limit_line_is_found_at_the_end_of_stderr():
    err = "warn: something\nmore noise\nError: 5-hour limit reached ∙ resets 3pm\n"
    assert usage_limited(None, err, now=NOW) == datetime(2026, 9, 30, 15, 0).timestamp()


def test_ordinary_output_and_errors_are_not_a_limit_hit():
    assert usage_limited(None, None) is None
    assert usage_limited("Built it.\n\nautopilot: result ready to merge", "") is None
    assert usage_limited("API Error: 429 rate_limit_error", "boom: auth failed") is None
    assert usage_limited("", "Error: Request timed out") is None
    # a long answer that talks about the limit (a worker on this very task) is not a hit
    essay = "I made usage_limited() read 'Claude AI usage limit reached|1790000000'. " * 20
    assert usage_limited(essay, "") is None
