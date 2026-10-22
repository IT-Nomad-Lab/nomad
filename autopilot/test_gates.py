"""Tests for the gate_network shadow probe (#1531): the merge queue's real git and fake gate
scripts (from test_merge).

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest autopilot -q
"""
import dataclasses
import os
import shutil
import sys

import pytest

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, NOMAD)

from autopilot import gates  # noqa: E402
from autopilot import merge as merge_mod  # noqa: E402
from autopilot.test_merge import Env, sh  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap not installed")


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def probe(env, project, **kw):
    seen, lines = [], []

    def runner(argv, cwd, log_path, timeout):
        seen.append((argv, cwd))
        return merge_mod.run_command(argv, cwd, log_path, timeout)

    gate = gates.probe(project, state_dir=str(env.tmp / "probe-state"), runner=runner,
                       out=lines.append, **kw)
    return gate, seen, "\n".join(lines)


def test_a_probe_runs_the_gate_without_network_on_the_default_tip_and_changes_nothing(env):
    project = env.project()
    project = dataclasses.replace(project, autopilot=dataclasses.replace(project.autopilot,
                                                                         gate_network="host"))
    gate, seen, text = probe(env, project)
    assert gate.ok and "p: PASS with gate_network none" in text
    assert seen and all(os.path.basename(a[0]) == "bwrap" and "--unshare-net" in a for a, _ in seen)
    wt = seen[0][1]
    assert os.path.dirname(wt) == str(env.wt_root) and not os.path.exists(wt)   # removed after
    assert sh(env.repo, "worktree", "list").count("\n") == 0                     # only the main one
    assert project.autopilot.gate_network == "host"                              # config untouched
    assert not os.path.exists(env.state_dir)          # no live wrappers rewritten, no merge state


def test_a_failing_step_is_reported_and_keep_leaves_the_worktree(env):
    env.config(tests={"T.A1_ok": "pass"}, step_exit=3)
    gate, seen, text = probe(env, env.project(), keep=True)
    assert not gate.ok and "p: FAIL with gate_network none" in text and "exited 3" in text
    assert os.path.isdir(seen[0][1]) and "kept the worktree" in text


def test_main_refuses_a_bad_command_line():
    assert gates.main(["gates"]) == 2
    assert gates.main(["gates", "run", "p"]) == 2
