"""Tests for the LiteLLM fallback alert.

What matters is when it speaks: a Claude alias served by codex or local, once per cooldown, and
never an exception into the completion. Every sender is a fake: nothing here reaches Zulip.

Run: python3 -m pytest litellm/test_fallback_alert.py -q
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fallback_alert as fa  # noqa: E402


class FakeSender:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, content):
        if self.fail:
            raise RuntimeError("zulip down")
        self.sent.append(content)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _alert(sender=None, cooldown=300, clock=None):
    return fa.FallbackAlert(sender=sender or FakeSender(), dry_run=False, cooldown=cooldown,
                            clock=clock or Clock())


@pytest.mark.parametrize("requested,served,tier", [
    ("deep", "codex", "codex"), ("balanced", "codex", "codex"),
    ("deep", "fast", "local"), ("balanced", "private", "local"), ("deep", "code", "local"),
])
def test_claude_to_codex_or_local_is_classified(requested, served, tier):
    assert fa.classify(requested, served) == tier


@pytest.mark.parametrize("requested,served", [
    ("deep", "balanced"),      # still Claude
    ("deep", "gpt"),           # metered cloud, out of scope
    ("codex", "fast"),         # not a Claude request
    ("gpt", "codex"),
    ("fast", "private"),
])
def test_other_fallbacks_are_quiet(requested, served):
    assert fa.classify(requested, served) is None
    s = FakeSender()
    assert _alert(s).handle(requested, served, background=False) is None
    assert s.sent == []


def test_message_names_requested_served_tier_and_time():
    s = FakeSender()
    msg = _alert(s).handle("deep", "codex", reason="RateLimitError: 5-hour limit", background=False)
    assert s.sent == [msg]
    assert "`deep`" in msg and "`codex` (codex)" in msg
    assert "Claude to codex" in msg
    assert "- at: " in msg
    assert "RateLimitError" in msg


def test_repeats_are_held_for_the_cooldown_then_sent_again():
    s, clock = FakeSender(), Clock()
    a = _alert(s, cooldown=300, clock=clock)
    assert a.handle("deep", "codex", background=False)
    clock.t = 100
    assert a.handle("deep", "codex", background=False) is None
    assert a.handle("deep", "fast", background=False)      # a different pair is its own alert
    clock.t = 301
    assert a.handle("deep", "codex", background=False)
    assert len(s.sent) == 3


def test_send_failure_never_raises():
    a = _alert(FakeSender(fail=True))
    assert a.handle("balanced", "fast", background=False)   # logged, not raised


def test_litellm_hook_reads_the_served_group_from_kwargs():
    s = FakeSender()
    a = _alert(s)
    a.handle = lambda r, sv, reason="", background=True: fa.FallbackAlert.handle(
        a, r, sv, reason, background=False)
    asyncio.run(a.log_success_fallback_event("deep", {"model": "codex"}, RuntimeError("throttled")))
    asyncio.run(a.log_success_fallback_event("deep", {"model": "balanced"}, None))
    asyncio.run(a.log_success_fallback_event("deep", None, None))   # malformed, still no raise
    assert len(s.sent) == 1 and "RuntimeError: throttled" in s.sent[0]


def test_dry_run_sends_nothing():
    s = FakeSender()
    a = fa.FallbackAlert(sender=s, dry_run=True, cooldown=0, clock=Clock())
    assert a.handle("deep", "codex", background=False)
    assert s.sent == []


def test_zuliprc_and_env_build_the_sender(tmp_path, monkeypatch):
    rc = tmp_path / "zuliprc"
    rc.write_text("[api]\nemail=bot@x\nkey=k\nsite=https://zulip.example:8446\n")
    monkeypatch.setenv("NOMAD_ZULIPRC", str(rc))
    monkeypatch.setenv("NOMAD_FALLBACK_ZULIP_URL", "http://host.docker.internal:8096")
    monkeypatch.delenv("NOMAD_FALLBACK_ZULIP_STREAM", raising=False)
    z = fa.Zulip.from_env()
    assert (z.stream, z.topic) == ("alerts", "litellm-fallback")
    assert z.url == "http://host.docker.internal:8096" and z.site == "https://zulip.example:8446"


def test_missing_zuliprc_gives_no_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("NOMAD_ZULIPRC", str(tmp_path / "absent"))
    assert fa.Zulip.from_env() is None


def test_cli_dry_run_prints_a_marked_test_message(capsys):
    assert fa.main(["--dry-run", "--served", "fast"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("**[TEST] LiteLLM fell back from Claude to local**")
    assert "`fast` (local)" in out
