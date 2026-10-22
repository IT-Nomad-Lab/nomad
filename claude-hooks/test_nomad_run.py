"""Tests for nomad-operator/bin/nomad-run, the launcher between a session and the hooks.

The Windows branch is tested without Windows: a fake `uname` on PATH says MINGW64, and a fake
`wsl.exe` does what the real one does with `-d <distro> -e <cmd...>`, which is run the command
(in this same Linux box, with the fakes taken off PATH), or fail when the distro doesn't exist.
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUN = os.path.join(ROOT, "nomad-operator", "bin", "nomad-run")

PUSH = {"tool_name": "Bash", "tool_input": {"command": "git -C x push"}}
GREEN = {"tool_name": "Bash", "tool_input": {"command": "git status"}}

FAKE_WSL = r"""#!/usr/bin/env bash
# Fake wsl.exe: -d <distro> -e <cmd...>. Logs its argv, then runs <cmd> with the fakes gone.
printf '%s\n' "$@" > "$FAKE_LOG"
[ "$1" = -d ] || exit 99
distro="$2"; shift 2
[ "$1" = -e ] || exit 98
shift
if [ "$distro" = does-not-exist ]; then
  printf 'W\0S\0L\0 \0e\0r\0r\0o\0r\0\n'                  # wsl.exe prints UTF-16 to stdout
  exit 1
fi
[ -n "${FAKE_SLOW:-}" ] && sleep "$FAKE_SLOW"
PATH="$REAL_PATH" exec "$@"
"""


@pytest.fixture
def env():
    base = dict(os.environ)
    for k in ("NOMAD_HOST", "NOMAD_WSL_DISTRO", "NOMAD_WSL_ROOT", "MSYS_NO_PATHCONV"):
        base.pop(k, None)
    base["NOMAD_PY"] = sys.executable
    return base


@pytest.fixture
def win(tmp_path, env):
    """Environment for the Windows branch: fake uname and wsl.exe first on PATH."""
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uname").write_text("#!/bin/sh\necho MINGW64_NT-10.0-26300\n")
    (fake / "wsl.exe").write_text(FAKE_WSL)
    for f in fake.iterdir():
        f.chmod(0o755)
    env = dict(env)
    env["REAL_PATH"] = env["PATH"]
    env["PATH"] = f"{fake}:{env['PATH']}"
    env["FAKE_LOG"] = str(tmp_path / "wsl-argv")
    env["NOMAD_WSL_ROOT"] = ROOT
    return env


def run(env, *args, payload=None, cwd=None):
    data = json.dumps(payload) if payload is not None else ""
    return subprocess.run(["bash", RUN, *args], input=data, capture_output=True, text=True,
                          env=env, timeout=30, cwd=cwd)


def decision(out):
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else None


# ── WSL branch ──

def test_wsl_tier_gate_asks_on_push(env):
    r = run(env, "tier-gate", payload=PUSH)
    assert r.returncode == 0 and decision(r.stdout) == "ask"


def test_wsl_tier_gate_matches_direct_run(env):
    """G2 groundwork: through the launcher or straight to Python, the bytes are the same."""
    direct = subprocess.run([sys.executable, os.path.join(HERE, "tier_gate.py")],
                            input=json.dumps(PUSH), capture_output=True, text=True)
    assert run(env, "tier-gate", payload=PUSH).stdout == direct.stdout


def test_wsl_green_passes_silently(env):
    r = run(env, "tier-gate", payload=GREEN)
    assert (r.returncode, r.stdout) == (0, "")


def test_unknown_target_refused(env):
    r = run(env, "rm-everything", payload=GREEN)
    assert r.returncode == 2 and r.stdout == "" and "unknown target" in r.stderr


def test_no_target_refused(env):
    assert run(env).returncode == 2


def test_wsl_track_outside_repo_is_silent(env, tmp_path):
    r = run(env, "track", "start", payload={"cwd": str(tmp_path)})
    assert (r.returncode, r.stdout) == (0, "")


def test_wsl_track_without_python_fails_open(env, tmp_path):
    env["NOMAD_PY"] = str(tmp_path / "missing-python")
    r = run(env, "track", "start", payload={"cwd": str(tmp_path)})
    assert (r.returncode, r.stdout) == (0, "")


def test_wsl_sets_host(env, tmp_path):
    # nomad-pm execs "$NOMAD_PY <root>/mcp-local/nomad_pm.py"; a shim stands in for Python
    shim = tmp_path / "py"
    shim.write_text('#!/bin/sh\necho "$NOMAD_HOST"\n')
    shim.chmod(0o755)
    env["NOMAD_PY"] = str(shim)
    assert run(env, "nomad-pm").stdout.strip() == "wsl"



@pytest.fixture
def cached(tmp_path):
    """The launcher alone, the way Claude Code copies a plugin into plugins/cache/ (#1054)."""
    bin_dir = tmp_path / "cache" / "nomad-marketplace" / "nomad-operator" / "0.1.0" / "bin"
    bin_dir.mkdir(parents=True)
    copy = bin_dir / "nomad-run"
    copy.write_text(open(RUN).read())
    return str(copy)


def run_copy(path, env, *args, payload=None):
    return subprocess.run(["bash", path, *args], input=json.dumps(payload or {}),
                          capture_output=True, text=True, env=env, timeout=30)


def test_cached_copy_falls_back_to_the_export(cached, env):
    env["NOMAD_WSL_ROOT"] = ROOT
    r = run_copy(cached, env, "tier-gate", payload=PUSH)
    assert r.returncode == 0 and decision(r.stdout) == "ask"
    r = run_copy(cached, env, "tier-gate", payload=GREEN)
    assert (r.returncode, r.stdout) == (0, "")


def test_cached_copy_default_export_is_under_home(cached, env, tmp_path):
    share = tmp_path / "home" / ".local" / "share"
    share.mkdir(parents=True)
    os.symlink(ROOT, share / "nomad-marketplace")
    env["HOME"] = str(tmp_path / "home")
    r = run_copy(cached, env, "tier-gate", payload=PUSH)
    assert r.returncode == 0 and decision(r.stdout) == "ask"


def test_cached_copy_without_export_asks_never_blocks(cached, env):
    """Exit 2 from a PreToolUse hook blocks the tool: a missing export must ask instead."""
    env["NOMAD_WSL_ROOT"] = "/nonexistent"
    r = run_copy(cached, env, "tier-gate", payload=GREEN)
    assert r.returncode == 0 and decision(r.stdout) == "ask" and "install.sh" in r.stderr


def test_cached_copy_without_export_track_is_silent(cached, env, tmp_path):
    env["NOMAD_WSL_ROOT"] = "/nonexistent"
    r = run_copy(cached, env, "track", "start", payload={"cwd": str(tmp_path)})
    assert (r.returncode, r.stdout) == (0, "")

# ── Windows branch (fake uname + wsl.exe) ──

def test_windows_push_asks_through_wsl(win):
    r = run(win, "tier-gate", payload=PUSH)
    assert r.returncode == 0 and decision(r.stdout) == "ask"


def test_windows_output_is_byte_identical_to_wsl(win, env):
    """G1 = G2 byte for byte."""
    assert run(win, "tier-gate", payload=PUSH).stdout == run(env, "tier-gate", payload=PUSH).stdout


def test_windows_calls_wsl_with_distro_and_exec(win, tmp_path):
    run(win, "tier-gate", payload=PUSH)
    argv = (tmp_path / "wsl-argv").read_text().splitlines()
    assert argv[:4] == ["-d", "Ubuntu-24.04", "-e", "bash"]
    # nomad-run <root> <the Windows cwd> <target>; off Windows, `pwd -W` falls back to pwd
    assert argv[-4:] == ["nomad-run", ROOT, os.getcwd(), "tier-gate"]


def test_windows_distro_override(win, tmp_path):
    win["NOMAD_WSL_DISTRO"] = "Other-1"
    run(win, "tier-gate", payload=GREEN)
    assert (tmp_path / "wsl-argv").read_text().splitlines()[:2] == ["-d", "Other-1"]


def test_windows_sets_host_and_no_pathconv(win, tmp_path):
    probe = tmp_path / "py"
    probe.write_text('#!/bin/sh\necho "$NOMAD_HOST $MSYS_NO_PATHCONV"\n')
    probe.chmod(0o755)
    # NOMAD_PY reaches the WSL side through the fake (same box); the real wsl.exe would need
    # WSLENV, which is why nomad-run passes the host inside the bash -c script instead.
    win["NOMAD_PY"] = str(probe)
    r = run(win, "track", "start", payload={})
    assert r.stdout.strip() == "windows 1"


def test_windows_green_passes_silently(win):
    r = run(win, "tier-gate", payload=GREEN)
    assert (r.returncode, r.stdout) == (0, "")


@pytest.mark.parametrize("why", ["distro", "root", "slow"])
def test_windows_unreachable_tier_gate_asks(win, why):
    """G6: no WSL, a broken install or a hang must ask, never pass."""
    if why == "distro":
        win["NOMAD_WSL_DISTRO"] = "does-not-exist"
    elif why == "root":
        win["NOMAD_WSL_ROOT"] = "/nonexistent"
    else:
        win["FAKE_SLOW"], win["NOMAD_RUN_TIMEOUT"] = "5", "1"
    r = run(win, "tier-gate", payload=GREEN)
    assert r.returncode == 0 and decision(r.stdout) == "ask"
    assert "\0" not in r.stdout


@pytest.mark.parametrize("why", ["distro", "slow"])
def test_windows_unreachable_track_is_silent(win, tmp_path, why):
    """G6: tracking fails open."""
    if why == "distro":
        win["NOMAD_WSL_DISTRO"] = "does-not-exist"
    else:
        win["FAKE_SLOW"], win["NOMAD_RUN_TIMEOUT"] = "5", "1"
    for mode in ("start", "record", "end"):
        r = run(win, "track", mode, payload={"cwd": str(tmp_path)})
        assert (r.returncode, r.stdout) == (0, "")


def test_windows_unknown_target_never_reaches_wsl(win, tmp_path):
    r = run(win, "bogus", payload=GREEN)
    assert r.returncode == 2 and not (tmp_path / "wsl-argv").exists()


# ── graphify (#1049): the CLI in WSL, in the caller's directory ──

FAKE_GRAPHIFY = '#!/bin/sh\necho "$(pwd) $NOMAD_HOST $*"\nexit "${FAKE_RC:-0}"\n'


@pytest.fixture
def graphify(tmp_path):
    g = tmp_path / "graphify"
    g.write_text(FAKE_GRAPHIFY)
    g.chmod(0o755)
    return str(g)


def test_wsl_graphify_runs_in_cwd(env, graphify, tmp_path):
    env["NOMAD_GRAPHIFY"] = graphify
    r = run(env, "graphify", "query", "how does x work", cwd=tmp_path)
    assert (r.returncode, r.stdout.strip()) == (0, f"{tmp_path} wsl query how does x work")


def test_wsl_graphify_missing_says_install_in_wsl(env, tmp_path):
    env.update(NOMAD_GRAPHIFY=str(tmp_path / "nope"), HOME=str(tmp_path), PATH="/usr/bin:/bin")
    r = run(env, "graphify", "query", "x")
    assert r.returncode == 1 and "Install it there with 'uv tool install graphifyy'" in r.stderr


def test_windows_graphify_runs_in_the_callers_dir(win, graphify, tmp_path):
    win["NOMAD_GRAPHIFY"] = graphify
    work = tmp_path / "proj"
    work.mkdir()
    r = run(win, "graphify", "update", ".", cwd=work)
    assert (r.returncode, r.stdout.strip()) == (0, f"{work} windows update .")


def test_windows_graphify_exit_code_and_no_timeout(win, graphify):
    """graphify is exec'd straight through, unlike the hooks: no timeout, its own exit code."""
    win.update(NOMAD_GRAPHIFY=graphify, FAKE_RC="3", FAKE_SLOW="2", NOMAD_RUN_TIMEOUT="1")
    r = run(win, "graphify", "query", "x")
    assert r.returncode == 3 and "query x" in r.stdout


@pytest.mark.parametrize("win_cwd", ["D:/not/mounted", "/c/no/such/dir"])
def test_graphify_unseen_windows_dir_fails(env, graphify, win_cwd):
    """The WSL side of a Windows call: a folder WSL can't see is an error, not a run elsewhere."""
    env.update(NOMAD_GRAPHIFY=graphify, NOMAD_WIN_CWD=win_cwd, NOMAD_HOST="windows")
    r = run(env, "graphify", "query", "x")
    assert r.returncode == 1 and "no such directory in WSL" in r.stderr and r.stdout == ""


@pytest.mark.parametrize("win_path, wsl_path", [
    ("C:/Users/josia/Projects/x", "/mnt/c/Users/josia/Projects/x"),
    ("C:\\Users\\josia", "/mnt/c/Users/josia"),
    ("/c/Users/josia", "/mnt/c/Users/josia"),
    ("/home/jdelimam/nomad", "/home/jdelimam/nomad"),
])
def test_winpath_cli(win_path, wsl_path):
    r = subprocess.run([sys.executable, os.path.join(HERE, "winpath.py"), win_path],
                       capture_output=True, text=True)
    assert r.stdout.strip() == wsl_path


# ── lane-mcp (#1056): the nomad plugin's lane MCP servers, in WSL ──

LANES = ("comms", "research", "content", "dev")
PLUGIN = os.path.join(ROOT, "nomad-plugin")
SHIM = os.path.join(PLUGIN, "bin", "nomad-run")


@pytest.fixture
def lane_py(tmp_path, env):
    """A Python stand-in that prints the script it was given and the settings the lane reads."""
    shim = tmp_path / "py"
    shim.write_text('#!/bin/sh\necho "$1|$NOMAD_ENV_FILE|$NOMAD_DISPATCH_URL|$NOMAD_HOST"\n')
    shim.chmod(0o755)
    for k in ("NOMAD_ENV_FILE", "NOMAD_DISPATCH_URL"):
        env.pop(k, None)
    env["NOMAD_PY"] = str(shim)
    return env


@pytest.mark.parametrize("lane", LANES)
def test_wsl_lane_mcp_runs_the_lane_with_defaults(lane_py, lane):
    r = run(lane_py, "lane-mcp", lane)
    script, envfile, dispatch, host = r.stdout.strip().split("|")
    assert r.returncode == 0
    assert script == os.path.join(ROOT, "nomad-plugin", "mcp", f"mcp_{lane}.py")
    assert os.path.exists(script)
    assert envfile == os.path.join(os.path.expanduser("~"), "nomad", ".env")
    assert (dispatch, host) == ("http://127.0.0.1:8090", "wsl")


def test_wsl_lane_mcp_keeps_overrides(lane_py):
    lane_py["NOMAD_ENV_FILE"], lane_py["NOMAD_DISPATCH_URL"] = "/x/.env", "http://d:1"
    assert run(lane_py, "lane-mcp", "dev").stdout.strip().split("|")[1:3] == ["/x/.env", "http://d:1"]


@pytest.mark.parametrize("lane", ["", "bogus", "../dev", "dev;id"])
def test_wsl_lane_mcp_unknown_lane_refused(lane_py, lane):
    r = run(lane_py, "lane-mcp", lane)
    assert r.returncode == 1 and r.stdout == "" and "unknown lane" in r.stderr


def test_windows_lane_mcp_execs_straight_into_wsl(win, lane_py, tmp_path):
    """A stdio server: no timeout, and its output and exit code come back as they are."""
    win["NOMAD_PY"], win["NOMAD_RUN_TIMEOUT"] = lane_py["NOMAD_PY"], "1"
    win.pop("NOMAD_ENV_FILE", None)
    win.pop("NOMAD_DISPATCH_URL", None)
    win["FAKE_SLOW"] = "2"
    r = run(win, "lane-mcp", "comms")
    assert r.returncode == 0
    assert r.stdout.strip().split("|")[0].endswith("nomad-plugin/mcp/mcp_comms.py")
    assert r.stdout.strip().endswith("|windows")
    assert "lane-mcp" in (tmp_path / "wsl-argv").read_text().split("\n")


def test_windows_lane_mcp_failure_is_not_hidden(win):
    win["NOMAD_WSL_DISTRO"] = "does-not-exist"
    assert run(win, "lane-mcp", "comms").returncode != 0


def run_shim(path, env, *args):
    return subprocess.run(["bash", path, *args], input="", capture_output=True, text=True,
                          env=env, timeout=30)


def test_plugin_shim_finds_the_launcher_beside_it(lane_py):
    r = run_shim(SHIM, lane_py, "lane-mcp", "research")
    assert r.returncode == 0 and r.stdout.startswith(os.path.join(ROOT, "nomad-plugin"))


def test_plugin_shim_in_a_cached_copy_uses_the_export(lane_py, tmp_path):
    copy = tmp_path / "cache" / "nomad-marketplace" / "nomad" / "0.1.0" / "bin" / "nomad-run"
    copy.parent.mkdir(parents=True)
    copy.write_text(open(SHIM).read())
    lane_py["HOME"], lane_py["NOMAD_WSL_ROOT"] = str(tmp_path), ROOT
    r = run_shim(str(copy), lane_py, "lane-mcp", "content")
    assert r.returncode == 0 and "mcp_content.py" in r.stdout


def test_plugin_shim_without_any_launcher_fails(env, tmp_path):
    copy = tmp_path / "bin" / "nomad-run"
    copy.parent.mkdir()
    copy.write_text(open(SHIM).read())
    env["HOME"] = str(tmp_path)
    env.pop("NOMAD_WSL_ROOT", None)
    env.pop("NOMAD_WIN_ROOT", None)
    r = run_shim(str(copy), env, "lane-mcp", "dev")
    assert r.returncode == 1 and "install.sh" in r.stderr


def test_plugin_mcp_json_routes_every_lane_through_the_launcher():
    servers = json.load(open(os.path.join(PLUGIN, ".mcp.json")))["mcpServers"]
    assert set(servers) == {f"nomad-{l}" for l in LANES}
    for name, s in servers.items():
        # git's alias runs Git Bash on Windows, never System32\bash.exe (as nomad-operator's)
        assert s["command"] == "git"
        assert s["args"] == ["-c", "alias.nomad-run=!bash", "nomad-run",
                             "${CLAUDE_PLUGIN_ROOT}/bin/nomad-run", "lane-mcp", name[6:]]


def test_plugin_has_no_hooks_and_no_python_in_commands():
    """The tier gate is the red tier (#1056), and the commands run in Git Bash without Python."""
    # the files, not the folder: a checkout keeps an ignored hooks/__pycache__/ after the merge
    assert not any(os.path.exists(os.path.join(PLUGIN, "hooks", f)) for f in ("hooks.json", "guard.py"))
    assert "hooks" not in json.load(open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")))
    cmds = os.path.join(PLUGIN, "commands")
    for f in os.listdir(cmds):
        assert "python" not in open(os.path.join(cmds, f)).read(), f
