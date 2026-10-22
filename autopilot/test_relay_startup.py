"""nomad-relay waits for a Zulip that isn't up yet instead of crashing at boot (#993).

Lives here, not in nomad-relay/tests, because only autopilot/ and dispatcher/ are in the declared
test gate (as #939 did). zulip itself is faked: relay.py imports it only inside main()."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "nomad-relay"))
import relay  # noqa: E402

OP, OP_ID, BOT = "op@example.com", "7", 42


class _Client:
    def __init__(self, key_ok=True):
        self.key_ok = key_ok

    def call_endpoint(self, url, method, **_kw):
        if not self.key_ok:
            return {"result": "error", "msg": "Invalid API key"}
        return {"result": "success", "user": {"user_id": int(OP_ID)}}

    def get_profile(self):
        return {"result": "success", "user_id": BOT}


def fake_zulip(fails, key_ok=True):
    """A zulip module whose Client() fails like a Zulip still booting, `fails` times."""
    z = types.SimpleNamespace(made=0)

    def client(config_file):
        z.made += 1
        if z.made <= fails:
            raise AssertionError("self.zulip_version is not None")
        return _Client(key_ok)
    z.Client = client
    return z


@pytest.fixture(autouse=True)
def operator(monkeypatch):
    monkeypatch.setattr(relay, "OPERATOR", OP)
    monkeypatch.setattr(relay, "OPERATOR_ID", OP_ID)


def test_connect_retries_with_backoff_then_succeeds():
    z, sleeps = fake_zulip(fails=6), []
    chat, operator_id, bot_id = relay.until_up(lambda: relay.connect(z), sleep=sleeps.append)
    assert (operator_id, bot_id) == (int(OP_ID), BOT)
    assert isinstance(chat, relay.Chat)
    assert z.made == 7
    assert sleeps == [5, 10, 20, 40, 60, 60]          # doubles, capped at CONNECT_CAP


def test_connect_first_try_does_not_sleep():
    sleeps = []
    relay.until_up(lambda: relay.connect(fake_zulip(fails=0)), sleep=sleeps.append)
    assert sleeps == []


def test_auth_error_at_startup_still_raises():
    """A wrong key must still crash-loop into `failed`, not wait forever looking healthy."""
    z, sleeps = fake_zulip(fails=0, key_ok=False), []
    with pytest.raises(RuntimeError, match="Invalid API key"):
        relay.until_up(lambda: relay.connect(z), sleep=sleeps.append)
    assert sleeps == []


def test_main_survives_zulip_not_ready(monkeypatch):
    z, sleeps, seen = fake_zulip(fails=3), [], {}
    monkeypatch.setitem(sys.modules, "zulip", z)
    monkeypatch.setattr(relay.time, "sleep", sleeps.append)
    monkeypatch.setattr(relay, "State", lambda: None)

    class FakeRelay:
        def __init__(self, engine, chat, state, operator_id, bot_id):
            seen.update(operator_id=operator_id, bot_id=bot_id)
            self.operator_id = operator_id
            self.wake = types.SimpleNamespace(set=lambda: None)
            self.sse_loop = self.runs_loop = self.slow_loop = lambda: None

        def check_org(self):
            pass

        def event_loop(self, client):
            seen["client"] = client
            raise SystemExit(0)

    monkeypatch.setattr(relay, "Relay", FakeRelay)
    with pytest.raises(SystemExit) as e:
        relay.main()
    assert e.value.code == 0
    assert seen["operator_id"] == int(OP_ID) and seen["bot_id"] == BOT
    assert isinstance(seen["client"], _Client)
    assert sleeps == [5, 10, 20]
