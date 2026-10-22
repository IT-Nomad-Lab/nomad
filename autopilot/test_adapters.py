"""Native seam characterization: preserving reviewed worker policy and process behavior."""
import subprocess
from unittest.mock import Mock

from autopilot.adapters import ClaudeAdapter


def test_claude_command_preserves_native_settings_caps_and_stream_format():
    adapter = ClaudeAdapter(parse_stream=Mock(), kill_group=Mock(), adopt=Mock())
    settings = ["--settings", "/tmp/generated.json", "--setting-sources", "", "--permission-mode", "dontAsk"]
    caps = ["--model", "sonnet", "--max-turns", "150", "--max-budget-usd", "10"]
    command = adapter.command("/fixture/claude", "required task prompt", settings, caps)
    assert command == ["/fixture/claude", "-p", "required task prompt", *settings, *caps,
                       "--verbose", "--output-format", "stream-json"]
    assert settings[0] == "--settings" and caps[0] == "--model"


def test_native_launch_keeps_detached_group_no_stdin_and_controller_environment():
    popen = Mock()
    adapter = ClaudeAdapter(parse_stream=Mock(), kill_group=Mock(), adopt=Mock(), popen=popen)
    out, err = object(), object()
    env = {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
    proc = adapter.launch(["fake", "-p", "task"], cwd="/tmp/worktree", stdout=out, stderr=err, env=env)
    popen.assert_called_once_with(["fake", "-p", "task"], cwd="/tmp/worktree", stdout=out,
                                 stderr=err, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    assert proc is popen.return_value


def test_native_parse_terminate_and_adopt_delegate_without_reinterpreting_evidence():
    parse, terminate, adopt = Mock(), Mock(), Mock()
    adapter = ClaudeAdapter(parse_stream=parse, kill_group=terminate, adopt=adopt)
    assert adapter.parse("fixture.jsonl") is parse.return_value
    parse.assert_called_once_with("fixture.jsonl")
    proc = object()
    adapter.terminate(proc)
    terminate.assert_called_once_with(proc)
    assert adapter.adopt(123, 456) is adopt.return_value
    adopt.assert_called_once_with(123, 456)
