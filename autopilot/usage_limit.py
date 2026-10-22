"""Is this Claude run over the Max plan's usage limit, and when does the limit reset? (#960)

    reset_at = usage_limited(result_text, stderr)    # epoch seconds, or None

Shared by the autopilot worker (autopilot/worker.py) and the mention sessions, so both read a
limit hit the same way. There is no paid fallback: a run that hits the limit waits for the reset
and continues (the operator's rule on #960; account "extra usage" is off).

The message formats. Claude Code has said the limit in more than one way, and no live hit has been
captured on this machine yet, so the matcher takes every form seen so far:
  - `Claude AI usage limit reached|1759248000`             the reset as epoch seconds
  - `Claude usage limit reached. Your limit will reset at 3pm (Europe/Paris).`
  - `5-hour limit reached ∙ resets 3pm`, `You've hit your limit · resets 3:30pm (Europe/Paris)`
  - `Weekly limit reached ∙ resets Oct 3, 9am`
A message with no reset time we can read resets at now + DEFAULT_WAIT (5 h, the plan's window).

A limit message is short. Anything longer than MAX_LEN is an ordinary answer that happens to talk
about limits (a worker on this very task, say), and never reads as a hit.
"""
import re
import time
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

DEFAULT_WAIT = 5 * 3600
MAX_LEN = 600

HIT = re.compile(
    r"(?:claude(?: ai)? usage limit reached"
    r"|(?:5-hour|five-hour|session|weekly|opus|sonnet|usage) limit reached"
    r"|you(?:'|’)?ve (?:hit|reached) your (?:usage )?limit"
    r"|you have (?:hit|reached) your (?:usage )?limit)", re.I)
EPOCH = re.compile(r"limit reached\|(\d{9,11})\b", re.I)
MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
RESET = re.compile(
    r"resets?(?:\s+at)?\s+"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2}),?"
    r"(?:\s+at)?\s+)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+\-]+)*)\))?", re.I)


def _zone(name):
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def parse_reset(text, now=None):
    """The reset time named in a limit message, as epoch seconds, or None when it names none.
    A clock time with no date is the next time the clock shows it, in the message's time zone
    (the machine's when it gives none)."""
    now = time.time() if now is None else now
    m = EPOCH.search(text)
    if m:
        return float(m.group(1))
    m = RESET.search(text)
    if not m:
        return None
    h, mins, ampm = int(m.group("h")), int(m.group("m") or 0), (m.group("ampm") or "").lower()
    if ampm:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ampm == "pm" else 0)
    elif not m.group("m"):
        return None                      # a bare number ("resets 5") is not a time
    if h > 23 or mins > 59:
        return None
    tz = _zone(m.group("tz"))
    base = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
    if m.group("mon"):
        try:
            at = base.replace(month=MONTHS[m.group("mon").lower()[:3]], day=int(m.group("day")),
                              hour=h, minute=mins, second=0, microsecond=0)
        except ValueError:
            return None
        if at.timestamp() <= now:
            at = at.replace(year=at.year + 1)
        return at.timestamp()
    at = base.replace(hour=h, minute=mins, second=0, microsecond=0)
    if at.timestamp() <= now:
        at += timedelta(days=1)
    return at.timestamp()


def usage_limited(result_text, stderr="", now=None):
    """The reset time (epoch seconds) when the run ended on the Max usage limit, else None.
    result_text is the run's final result (or last message), stderr its error output; either may
    be None. No readable reset time gives now + DEFAULT_WAIT."""
    now = time.time() if now is None else now
    # stderr may carry other noise before the limit line: its last few lines are read one by one
    for text in [result_text, *(stderr or "").strip().splitlines()[-5:]]:
        text = (text or "").strip()
        if not text or len(text) > MAX_LEN or not HIT.search(text):
            continue
        at = parse_reset(text, now)
        return at if at is not None and at > now else now + DEFAULT_WAIT
    return None


def clock(epoch):
    """HH:MM, local time: how a reset time is shown to the operator."""
    return time.strftime("%H:%M", time.localtime(epoch))
