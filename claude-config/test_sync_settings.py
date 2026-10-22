"""Tests for sync_settings.py (#1053), on copies of both hosts' real settings (fixtures/, env
values redacted). Gates: G11 (idempotent, unowned keys kept), G10 (old hooks gone, so the tier
gate runs once), G6a (fixture tests)."""
import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sync_settings as ss  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


@pytest.fixture
def home(tmp_path):
    """A ~/.claude with the given fixture as settings.json; plugin installed unless told not."""
    def make(fixture, plugin=True):
        d = tmp_path / ".claude"
        (d / "plugins").mkdir(parents=True)
        shutil.copy(os.path.join(FIX, fixture), d / "settings.json")
        if plugin:
            (d / "plugins" / "installed_plugins.json").write_text(json.dumps(
                {"version": 2, "plugins": {ss.PLUGIN: [{"scope": "user"}]}}))
        return d
    return make


def run(d, host, *extra):
    return ss.main(["--host", host, "--settings", str(d / "settings.json"), *extra])


def read(d):
    return json.loads((d / "settings.json").read_text())


def commands(settings):
    return [h["command"] for groups in (settings.get("hooks") or {}).values()
            for g in groups for h in g.get("hooks", [])]


@pytest.mark.parametrize("fixture,host", [("wsl-settings.json", "wsl"),
                                          ("windows-settings.json", "windows")])
def test_second_run_changes_nothing(home, fixture, host, capsys):
    d = home(fixture)
    run(d, host)
    first = (d / "settings.json").read_bytes()
    baks = sorted(p.name for p in d.iterdir() if ".bak-" in p.name)
    assert len(baks) == 1
    run(d, host)
    assert (d / "settings.json").read_bytes() == first
    assert sorted(p.name for p in d.iterdir() if ".bak-" in p.name) == baks
    assert "no change" in capsys.readouterr().out


def test_wsl_keeps_unowned_and_sets_owned(home):
    before = json.load(open(os.path.join(FIX, "wsl-settings.json")))
    d = home("wsl-settings.json")
    run(d, "wsl")
    s = read(d)
    for k in ("tui", "theme", "env", "deniedMcpServers"):
        assert s[k] == before[k]
    assert s["permissions"]["defaultMode"] == "auto"
    assert s["permissions"]["ask"] == before["permissions"]["ask"]
    assert "Bash(docker compose:*)" in s["permissions"]["allow"]          # host-only rule kept
    assert len(s["permissions"]["allow"]) == len(set(s["permissions"]["allow"]))
    assert s["autoMode"]["environment"] == before["autoMode"]["environment"]
    assert s["autoMode"]["soft_deny"] == before["autoMode"]["soft_deny"]
    assert s["enabledPlugins"][ss.PLUGIN] is True
    assert s["enabledPlugins"]["unity@claude-plugins-official"] is False  # not owned, kept


def test_wsl_old_hooks_retired_so_gate_runs_once(home):
    """G10: only the plugin's tier-gate remains, so one Bash call runs it once."""
    d = home("wsl-settings.json")
    assert any("tier-gate.py" in c for c in commands(read(d)))
    run(d, "wsl")
    s = read(d)
    assert commands(s) == []
    assert "hooks" not in s


def test_old_hooks_kept_until_plugin_installed(home, capsys):
    d = home("wsl-settings.json", plugin=False)
    run(d, "wsl")
    assert any("tier-gate.py" in c for c in commands(read(d)))
    assert "isn't installed yet" in capsys.readouterr().out
    (d / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {ss.PLUGIN: [{"scope": "user"}]}}))
    run(d, "wsl")
    assert commands(read(d)) == []


def test_windows_ntfy_ps1_retired(home):
    before = json.load(open(os.path.join(FIX, "windows-settings.json")))
    d = home("windows-settings.json")
    run(d, "windows")
    s = read(d)
    assert commands(s) == []
    assert not any("ntfy.ps1" in r for r in s["permissions"]["allow"])
    assert "PowerShell(git status *)" in s["permissions"]["allow"]
    assert "Bash(git status:*)" in s["permissions"]["allow"]
    assert s["enabledPlugins"]["hostinger@claude-plugins-official"] is True
    assert s["autoUpdatesChannel"] == before["autoUpdatesChannel"]
    assert s["theme"] == "auto"


def test_dropped_item_removed_only_if_we_set_it(home, monkeypatch):
    d = home("wsl-settings.json")
    run(d, "wsl")
    want, needles = ss.config("wsl")
    fewer = json.loads(json.dumps(want))
    fewer["permissions"]["allow"].remove("WebSearch")              # was already there before
    fewer["permissions"]["allow"].remove("Bash(claude --version)")
    del fewer["agentPushNotifEnabled"]
    monkeypatch.setattr(ss, "config", lambda host: (fewer, needles))
    run(d, "wsl")
    s = read(d)
    assert "WebSearch" not in s["permissions"]["allow"]
    assert "Bash(claude --version)" not in s["permissions"]["allow"]
    assert "agentPushNotifEnabled" not in s
    assert "Bash(docker compose:*)" in s["permissions"]["allow"]


def test_first_run_never_removes(home):
    """No state file yet: nothing is unset, even items claude-config doesn't list."""
    d = home("wsl-settings.json")
    run(d, "wsl")
    assert "Bash(/home/jdelimam/bin/blender:*)" in read(d)["permissions"]["allow"]


def test_dry_run_writes_nothing_and_masks(home, capsys):
    d = home("wsl-settings.json")
    raw = (d / "settings.json").read_bytes()
    s = json.loads(raw)
    s["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] = "visible-value"
    s["mcpToken"] = "tok-123"
    (d / "settings.json").write_text(json.dumps(s))
    raw = (d / "settings.json").read_bytes()
    run(d, "wsl", "--dry-run")
    out = capsys.readouterr().out
    assert (d / "settings.json").read_bytes() == raw
    assert not (d / ss.STATE).exists()
    assert "tier-gate.py" in out and out.count("\n-") >= 1
    assert "visible-value" not in out and "tok-123" not in out


def test_missing_settings_file_is_created(tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    ss.main(["--host", "windows", "--settings", str(p)])
    s = json.loads(p.read_text())
    assert s["agentPushNotifEnabled"] is True
    assert not list(p.parent.glob("*.bak-*"))


def test_windows_needs_settings_path():
    with pytest.raises(SystemExit):
        ss.main(["--host", "windows"])


def test_windows_path_translated(home, monkeypatch):
    d = home("windows-settings.json")
    mount = str(d.parent / "mnt")
    target = os.path.join(mount, "c", "Users", "u", ".claude")
    shutil.copytree(d, target)
    monkeypatch.setenv("NOMAD_WSL_MOUNT", mount)
    for p in ("C:/Users/u/.claude/settings.json", r"C:\Users\u\.claude\settings.json",
              "/c/Users/u/.claude/settings.json"):
        ss.main(["--host", "windows", "--settings", p])
    s = json.loads(open(os.path.join(target, "settings.json")).read())
    assert commands(s) == [] and s["agentPushNotifEnabled"] is True


def test_through_nomad_run(home):
    d = home("windows-settings.json")
    root = os.path.dirname(HERE)
    r = subprocess.run(["bash", os.path.join(root, "nomad-operator", "bin", "nomad-run"),
                        "sync-settings", "--host", "windows", "--settings", str(d / "settings.json")],
                       capture_output=True, text=True, env=dict(os.environ, NOMAD_HOST="windows"))
    assert r.returncode == 0, r.stderr
    assert "updated" in r.stdout
    assert commands(read(d)) == []
