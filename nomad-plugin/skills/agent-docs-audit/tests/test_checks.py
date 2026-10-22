"""Tests for agent-docs-audit's checks.py (#1015). Every check runs against throwaway dirs and git
repos under tmp_path, never the real ~/.claude or ~/nomad.

    uv run --no-project --with pytest python -m pytest -q -p no:cacheprovider nomad-plugin/skills/agent-docs-audit/tests
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import checks  # noqa: E402


def sh(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    sh(r, "init", "-q", "-b", "master")
    (r / "README.md").write_text("x\n")
    sh(r, "add", ".")
    sh(r, "commit", "-q", "-m", "init")
    return r


def joined(rows):
    return "\n".join(rows)


# ── (a) size ────────────────────────────────────────────────────────────────────────────────

def test_size_flags_docs_over_budget_and_passes_small_ones(tmp_path, repo):
    (repo / "CLAUDE.md").write_text("x" * 5000)
    (repo / "Nomad.md").write_text("small\n")
    out = joined(checks.size_budget([str(repo)], str(tmp_path / "cfg")))
    assert "CLAUDE.md" in out and "Nomad.md" not in out
    assert "4 KB" in out


def test_size_flags_a_long_memory_index(tmp_path):
    mem = tmp_path / "cfg" / "projects" / "p" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- line\n" * 250)
    out = joined(checks.size_budget([], str(tmp_path / "cfg")))
    assert "250 lines" in out


def test_size_is_quiet_when_all_is_well(tmp_path, repo):
    assert checks.size_budget([str(repo)], str(tmp_path / "cfg")) == ["size: every doc is within budget"]


# ── (b) memory ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def memdir(tmp_path):
    mem = tmp_path / "cfg" / "projects" / "-home-x" / "memory"
    mem.mkdir(parents=True)
    return mem


def test_memory_finds_missing_index_targets_and_unlisted_files(tmp_path, memdir):
    (memdir / "MEMORY.md").write_text("- [A](a.md) hook\n- [Gone](gone.md) hook\n")
    (memdir / "a.md").write_text("fine\n")
    (memdir / "orphan.md").write_text("not indexed\n")
    out = joined(checks.stale_memory(str(tmp_path / "cfg")))
    assert "missing file: gone.md" in out
    assert "not in the index: orphan.md" in out
    assert "a.md\n" not in out


def test_memory_finds_paths_that_no_longer_exist(tmp_path, memdir):
    real = tmp_path / "real.txt"
    real.write_text("x")
    (memdir / "MEMORY.md").write_text("- [A](a.md) hook\n")
    (memdir / "a.md").write_text(f"see {real} and /home/nobody-here/gone/file.py, glob /home/x/*.md\n")
    out = joined(checks.stale_memory(str(tmp_path / "cfg")))
    assert "/home/nobody-here/gone/file.py" in out
    assert str(real) not in out
    assert "*.md" not in out                         # patterns are not paths


def test_memory_is_quiet_when_all_is_well(tmp_path, memdir):
    (memdir / "MEMORY.md").write_text("- [A](a.md) hook\n")
    (memdir / "a.md").write_text("nothing path-like\n")
    assert checks.stale_memory(str(tmp_path / "cfg"))[0].startswith("memory: every index entry")


# ── (c) drift ───────────────────────────────────────────────────────────────────────────────

def test_drift_compares_an_installed_skill_with_its_source_on_master(tmp_path, repo):
    src = repo / "skills" / "demo"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("v1\n")
    (src / "tests").mkdir()
    (src / "tests" / "test_x.py").write_text("pass\n")
    sh(repo, "add", ".")
    sh(repo, "commit", "-q", "-m", "skill")
    inst = tmp_path / "cfg" / "skills" / "demo"
    inst.mkdir(parents=True)
    (inst / "SOURCE.md").write_text(f"repo: {repo}\npath: skills/demo\ncommit: abc\n")
    (inst / "SKILL.md").write_text("v1\n")
    assert checks.drift([], str(tmp_path / "cfg"))[0].startswith("drift: no installed copy")  # tests/ not installed
    (inst / "SKILL.md").write_text("edited by hand\n")
    out = joined(checks.drift([], str(tmp_path / "cfg")))
    assert "differs from" in out and "SKILL.md" in out


def test_drift_checks_a_managed_by_stamp(tmp_path, repo):
    (tmp_path / "src.md").write_text("# doc\nbody\n")
    (repo / "CLAUDE.md").write_text(f"<!-- managed by {tmp_path / 'src.md'} -->\n# doc\nchanged\n")
    out = joined(checks.drift([str(repo)], str(tmp_path / "cfg")))
    assert "differs from its source" in out


# ── (d) worktrees ───────────────────────────────────────────────────────────────────────────

def test_worktrees_reports_merged_and_missing_but_not_unmerged_or_unstarted(tmp_path, repo):
    wt = tmp_path / "proj-wt"
    wt.mkdir()
    sh(repo, "worktree", "add", "-q", "-b", "done", str(wt / "done"))
    (wt / "done" / "f").write_text("x")
    sh(wt / "done", "add", ".")
    sh(wt / "done", "commit", "-q", "-m", "work")
    sh(repo, "merge", "-q", "--no-ff", "done", "-m", "merge")
    sh(repo, "worktree", "add", "-q", "-b", "open", str(wt / "open"))
    (wt / "open" / "g").write_text("x")
    sh(wt / "open", "add", ".")
    sh(wt / "open", "commit", "-q", "-m", "wip")
    sh(repo, "worktree", "add", "-q", "-b", "fresh", str(wt / "fresh"))   # at master's tip: not started
    sh(repo, "worktree", "add", "-q", "-b", "vanished", str(wt / "vanished"))
    subprocess.run(["rm", "-rf", str(wt / "vanished")], check=True)

    out = joined(checks.worktree_hygiene([str(repo)], str(tmp_path / "*-wt")))
    assert "branch done) is merged into master" in out
    assert "vanished" in out and "directory is gone" in out
    assert "branch open" not in out and "branch fresh" not in out
    assert (wt / "done").is_dir()                      # report only: nothing was removed


def test_worktrees_ignores_ones_outside_the_wt_dirs(tmp_path, repo):
    sh(repo, "worktree", "add", "-q", "-b", "elsewhere", str(tmp_path / "other" / "x"))
    assert checks.worktree_hygiene([str(repo)], str(tmp_path / "*-wt"))[0].startswith("worktrees: none")


# ── repos and main ──────────────────────────────────────────────────────────────────────────

def test_projects_yaml_paths_are_parsed_without_yaml(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    hub.mkdir()
    sh(hub, "init", "-q", "-b", "master")
    for name in ("a", "b/c"):
        d = tmp_path / name
        d.mkdir(parents=True)
        sh(d, "init", "-q")
    (hub / "projects.yaml").write_text(
        "# path: commented/out\nprojects:\n  - id: a\n    path: a\n"
        "  - {id: c, path: b/c, dispatch: false}   # inline\n  - {id: z, path: missing}\n")
    sh(hub, "add", ".")
    sh(hub, "commit", "-q", "-m", "p")
    monkeypatch.setattr(checks, "HOME", str(tmp_path))
    assert checks.repos_from_projects(str(hub)) == [str(tmp_path / "a"), str(tmp_path / "b/c")]


def test_main_runs_every_check_and_exits_zero(tmp_path, repo, capsys):
    assert checks.main(["all", "--repo", str(repo), "--config-dir", str(tmp_path / "cfg"),
                        "--wt-glob", str(tmp_path / "*-wt")]) == 0
    out = capsys.readouterr().out
    for name in ("size", "memory", "drift", "worktrees"):
        assert f"== {name}" in out
