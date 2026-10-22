"""Tests for nomad-operator/hooks/ntfy.sh. A fake curl on PATH logs its argv instead of sending."""
import json
import os
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
NTFY = os.path.join(os.path.dirname(HERE), "nomad-operator", "hooks", "ntfy.sh")
TOPIC = "test-topic-not-real"


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "curl").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CURL_LOG"\n')
    (fake / "curl").chmod(0o755)
    home = tmp_path / "home"
    (home / ".config" / "ntfy").mkdir(parents=True)
    (home / ".config" / "ntfy" / "topic").write_text(TOPIC + "\n")
    e = dict(os.environ, HOME=str(home), PATH=f"{fake}:{os.environ['PATH']}",
             CURL_LOG=str(tmp_path / "curl-argv"))
    e.pop("NTFY_SERVER", None)
    return e


def run(env, *args, payload=None):
    data = json.dumps(payload) if payload is not None else ""
    return subprocess.run(["bash", NTFY, *args], input=data, capture_output=True, text=True,
                          env=env, timeout=15)


def sent(env):
    """(headers, body, url) of the push, or None when curl never ran."""
    if not os.path.exists(env["CURL_LOG"]):
        return None
    argv = open(env["CURL_LOG"]).read().splitlines()
    headers = [argv[i + 1] for i, a in enumerate(argv) if a == "-H"]
    body = argv[argv.index("--data-binary") + 1]
    return headers, body, argv[-1]


def test_notification(env):
    r = run(env, payload={"hook_event_name": "Notification", "message": "Claude needs approval",
                          "cwd": "/home/u/proj"})
    headers, body, url = sent(env)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert headers == ["Title: Claude needs you (wsl)", "Tags: robot,wsl"]
    assert body == "Claude needs approval - proj"
    assert url == f"https://ntfy.sh/{TOPIC}"


def test_stop(env):
    run(env, payload={"hook_event_name": "Stop", "stop_hook_active": False, "cwd": "/x/nomad"})
    headers, body, _ = sent(env)
    assert headers[0] == "Title: Claude finished (wsl)" and body == "Turn done - nomad"


def test_stop_hook_active_is_skipped(env):
    r = run(env, payload={"hook_event_name": "Stop", "stop_hook_active": True})
    assert r.returncode == 0 and sent(env) is None


def test_windows_cwd_leaf_and_host(env, tmp_path):
    (tmp_path / "fakebin" / "uname").write_text("#!/bin/sh\necho MINGW64_NT-10.0\n")
    (tmp_path / "fakebin" / "uname").chmod(0o755)
    run(env, payload={"hook_event_name": "Stop", "cwd": "C:\\Users\\josia\\Projects\\bootalert"})
    headers, body, _ = sent(env)
    assert headers == ["Title: Claude finished (windows)", "Tags: robot,windows"]
    assert body == "Turn done - bootalert"


def test_manual_args(env):
    run(env, "parity", "test")
    headers, body, _ = sent(env)
    assert headers[0] == "Title: parity (wsl)" and body == "test"


def test_message_with_quotes(env):
    run(env, payload={"hook_event_name": "Notification", "message": 'run "git push"?'})
    assert sent(env)[1] == 'run \\"git push\\"?'


@pytest.mark.parametrize("payload", [{"hook_event_name": "PreToolUse"}, "not json"])
def test_other_events_and_junk_are_silent(env, payload):
    r = subprocess.run(["bash", NTFY], input=payload if isinstance(payload, str) else json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=15)
    assert (r.returncode, r.stdout) == (0, "") and sent(env) is None


def test_no_topic_no_push(env):
    os.remove(os.path.join(env["HOME"], ".config", "ntfy", "topic"))
    r = run(env, "parity", "test")
    assert r.returncode == 0 and sent(env) is None


def test_curl_failure_still_exits_0(env, tmp_path):
    (tmp_path / "fakebin" / "curl").write_text("#!/bin/sh\necho boom >&2\nexit 7\n")
    r = run(env, "parity", "test")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_topic_never_printed(env):
    r = run(env, payload={"hook_event_name": "Notification", "message": "m"})
    assert TOPIC not in r.stdout + r.stderr
