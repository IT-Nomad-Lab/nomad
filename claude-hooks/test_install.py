"""Tests for install.sh, the installer for both hosts (#1054), on the WSL path.

Hermetic: the installer exports a temp git repo holding a copy of this tree, into a temp HOME,
and a fake `claude` on PATH logs its argv and answers the list calls. The smoke step runs track
with the mcp-local venv, so these tests skip where that venv is missing.
"""
import json
import os
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INSTALL = os.path.join(HERE, "install.sh")
VENV_PY = os.path.expanduser("~/nomad/mcp-local/.venv/bin/python")

FAKE_CLAUDE = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$*" in
  "plugin marketplace list --json") cat "$FAKE_MKT" 2>/dev/null || echo '[]' ;;
  "plugin list --json") cat "$FAKE_PLUGINS" 2>/dev/null || echo '[]' ;;
  "mcp get penpot") exit 1 ;;
esac
exit 0
"""

OLD_SETTINGS = {
    "model": "x",
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": 'python3 "$HOME/.claude/hooks/tier-gate.py"'}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command":
            '"$HOME/nomad/mcp-local/.venv/bin/python" "$HOME/.claude/hooks/nomad-track/track.py" start'}]}],
    },
}


def export_paths():
    with open(INSTALL) as f:
        m = re.search(r"^paths=\(([^)]*)\)", f.read(), re.M)
    assert m, "install.sh has no paths=(...)"
    return m.group(1).split()


def test_export_has_every_dir_nomad_run_uses():
    """The launcher runs code from the export: each $root/<dir> it names must be exported."""
    with open(os.path.join(ROOT, "nomad-operator", "bin", "nomad-run")) as f:
        used = set(re.findall(r"\$root/([\w.-]+)/", f.read()))
    assert used and used <= set(export_paths())


def test_export_paths_exist():
    for p in export_paths():
        assert os.path.exists(os.path.join(ROOT, p)), p


def test_ps1_command_has_no_double_quotes():
    """Windows PowerShell 5.1 mangles embedded double quotes passed to a native program."""
    with open(os.path.join(HERE, "install.ps1")) as f:
        line = next(l for l in f if l.startswith("$run = "))
    assert '"' not in line and "${NOMAD_REF:-master}:claude-hooks/install.sh" in line


@pytest.fixture
def box(tmp_path):
    if not os.path.exists(VENV_PY):
        pytest.skip("no mcp-local venv")
    repo = tmp_path / "repo"
    for p in export_paths():
        src = os.path.join(ROOT, p)
        shutil.copytree(src, repo / p, ignore=shutil.ignore_patterns(".venv", "__pycache__"))
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git[:3] + ["init", "-q", "-b", "master"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "export"], check=True)

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(FAKE_CLAUDE)
    (fake / "claude").chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("NOMAD_", "CLAUDE_"))}
    env.update(HOME=str(home), NOMAD_REPO=str(repo), NOMAD_PY=VENV_PY,
               PATH=f"{fake}:{env['PATH']}", FAKE_LOG=str(tmp_path / "claude.log"),
               FAKE_MKT=str(tmp_path / "mkt.json"), FAKE_PLUGINS=str(tmp_path / "plugins.json"))

    log = tmp_path / "claude.log"
    return SimpleNamespace(
        home=home, env=env, tmp=tmp_path,
        settings=home / ".claude" / "settings.json",
        export=home / ".local" / "share" / "nomad-marketplace",
        run=lambda *a: subprocess.run(["bash", INSTALL, *a], capture_output=True, text=True,
                                      env=env, timeout=120),
        log=lambda: log.read_text().splitlines())


def test_dry_run_changes_nothing(box):
    box.settings.write_text(json.dumps(OLD_SETTINGS))
    before = box.settings.read_text()
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would run: claude plugin marketplace add" in r.stdout
    assert "smoke test: push asks, OK" in r.stdout and "#1006, OK" in r.stdout
    assert box.settings.read_text() == before
    assert not box.export.exists()
    assert all(l.endswith("--json") or l == "mcp get penpot" for l in box.log())


def test_install_adds_marketplace_and_plugin(box):
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"plugin marketplace add {box.export} --scope user" in box.log()
    assert "plugin install nomad-operator@nomad-marketplace -s user" in box.log()
    assert "plugin install nomad@nomad-marketplace -s user" in box.log()
    assert (box.export / "claude-hooks" / "tier_gate.py").exists()
    s = json.loads(box.settings.read_text())
    assert s["enabledPlugins"]["nomad-operator@nomad-marketplace"] is True
    assert s["enabledPlugins"]["nomad@nomad-marketplace"] is True


def test_rerun_updates_instead(box):
    (box.tmp / "mkt.json").write_text('[{"name": "nomad-marketplace"}]')
    (box.tmp / "plugins.json").write_text(
        '[{"id": "nomad-operator@nomad-marketplace"}, {"id": "nomad@nomad-marketplace"}]')
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "plugin marketplace update nomad-marketplace" in box.log()
    assert "plugin update nomad-operator@nomad-marketplace" in box.log()
    assert "plugin update nomad@nomad-marketplace" in box.log()
    assert not any(" add " in l or " install " in l for l in box.log())


def test_nomad_plugin_installed_beside_an_existing_operator(box):
    """The first rerun after #1056: nomad-operator is there, nomad isn't yet."""
    (box.tmp / "mkt.json").write_text('[{"name": "nomad-marketplace"}]')
    (box.tmp / "plugins.json").write_text('[{"id": "nomad-operator@nomad-marketplace"}]')
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "plugin update nomad-operator@nomad-marketplace" in box.log()
    assert "plugin install nomad@nomad-marketplace -s user" in box.log()


def test_old_copies_retired_once_unreferenced(box):
    box.settings.write_text(json.dumps(OLD_SETTINGS))
    hooks = box.home / ".claude" / "hooks"
    (hooks / "nomad-track").mkdir(parents=True)
    (hooks / "tier-gate.py").write_text("old")
    (hooks / "nomad-track" / "track.py").write_text("old")
    plugins = box.home / ".claude" / "plugins"
    plugins.mkdir()
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"nomad-operator@nomad-marketplace": [{"scope": "user"}]}}))
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "tier-gate.py" not in box.settings.read_text()
    assert not (hooks / "tier-gate.py").exists() and not (hooks / "nomad-track").exists()
    (backup,) = (box.home / ".claude" / "backups").iterdir()
    assert (backup / "tier-gate.py").read_text() == "old"


def test_old_copies_kept_while_referenced(box):
    """Without the plugin installed, sync-settings keeps the old entries, so the copies stay."""
    box.settings.write_text(json.dumps(OLD_SETTINGS))
    hooks = box.home / ".claude" / "hooks"
    hooks.mkdir()
    (hooks / "tier-gate.py").write_text("old")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert (hooks / "tier-gate.py").exists() and "kept" in r.stdout


def test_failed_export_keeps_the_last_good_one(box):
    assert box.run("--export-only").returncode == 0
    box.env["NOMAD_REF"] = "no-such-ref"
    r = box.run("--export-only")
    assert r.returncode != 0
    assert (box.export / "claude-hooks" / "tier_gate.py").exists()
