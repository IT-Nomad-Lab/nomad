"""Tests for autopilot/permissions.py: the permission template and per-project generator (#875).

Run: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot -q
The real-CLI check is opt-in (it runs a small model session):
  AUTOPILOT_CLAUDE_E2E=1 uv run --no-project --with pytest --with pyyaml python -m pytest \
      autopilot/test_permissions.py -q -k claude
"""
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, NOMAD)

from autopilot import permissions as P  # noqa: E402
from autopilot import projects  # noqa: E402
from autopilot.projects import ConfigError, Project  # noqa: E402

REAL = os.path.join(NOMAD, "projects.yaml")
SNAPSHOT = os.path.join(os.path.dirname(__file__), "testdata", "runematch-settings.json")
RM_REPO = "/mnt/c/Users/josia/Projects/RuneMatch"
RM_WT = "/mnt/c/Users/josia/Projects/RuneMatch-wt/ap-833"
STATE = "/home/op/.local/state/nomad-autopilot"


def _project(tmp_path, block, pid="p"):
    """A project with its repo at <tmp>/<pid> and the given autopilot block (YAML)."""
    import yaml
    repo = tmp_path / pid
    repo.mkdir(exist_ok=True)
    ap = projects.parse_autopilot(pid, str(repo), yaml.safe_load(textwrap.dedent(block)))
    return Project(id=pid, path=str(repo), autopilot=ap)


def _runematch():
    """RuneMatch from this checkout's registry. Its path is pinned to ~/runematch: from a
    worktree the registry would resolve it next to the worktree instead."""
    p = projects.get("runematch", REAL)
    p = dataclasses.replace(p, path=os.path.expanduser("~/runematch"))
    if p.repo != RM_REPO:
        pytest.skip("~/runematch does not point at the RuneMatch checkout on this machine")
    return p


@pytest.fixture
def rm_settings():
    return P.build_settings(_runematch(), RM_WT, state_dir=STATE)


# ---- the template itself ------------------------------------------------------------------------

def test_template_has_only_edit_rules_for_files_and_no_bare_edit():
    t = P.load_template()["permissions"]
    rules = t["allow"] + t["deny"]
    assert not [r for r in rules if r.startswith(("Write(", "NotebookEdit(", "MultiEdit("))], \
        "Write(...) rules are ignored by Claude Code; use Edit(...)"
    assert "Edit" not in t["allow"] and "Write" not in t["allow"], \
        "edits are allowed only inside the worktree"
    assert t["defaultMode"] == "dontAsk"


def test_template_allows_no_raw_project_or_escape_commands():
    t = P.load_template()["permissions"]
    for bad in ("Bash(flock", "Bash(git -C:", "Bash(git:", "Bash(env", "Bash(sh ", "Bash(bash",
                "Bash(scripts/", "Bash(./", "Bash(rm", "Bash(git checkout", "Bash(git push"):
        assert not [r for r in t["allow"] if r.startswith(bad)], bad


# ---- RuneMatch snapshot ------------------------------------------------------------------------

def test_runematch_settings_match_the_snapshot(rm_settings):
    with open(SNAPSHOT) as f:
        assert rm_settings == json.load(f), \
            "regenerate with: python -m autopilot.test_permissions --snapshot, then review the diff"


def test_runematch_wrappers_and_directories(rm_settings):
    p = rm_settings["permissions"]
    b = f"{STATE}/bin/runematch-"
    assert [r for r in p["allow"] if STATE in r] == [
        f"Bash({b}test-EditMode)", f"Bash({b}test-EditMode *)",
        f"Bash({b}test-PlayMode)", f"Bash({b}test-PlayMode *)",
        f"Bash({b}gate-1)", f"Bash({b}post-test-1)"]
    assert p["additionalDirectories"] == [RM_WT, RM_REPO, "/tmp"]
    for g in ("**/*.unity", "**/*.prefab"):
        assert f"Edit({g})" in p["deny"]


# ---- denies ----------------------------------------------------------------------------------

DENIED = [
    "git push", "git push origin master", f"git -C {RM_WT} push origin feat/x",
    f"git -C {RM_REPO} push", "git stash", "git stash pop", f"git -C {RM_WT} stash push -m x",
    "git reset --hard HEAD~1", f"git -C {RM_WT} reset --hard", f"git -C {RM_REPO} reset --hard",
    "git rebase master", f"git -C {RM_WT} rebase master", "git clean -fdx",
    f"git -C {RM_WT} clean -fd", "git branch -D feat/x", f"git -C {RM_WT} branch -D feat/x",
    f"git -C {RM_WT} branch --delete x", "git commit --amend --no-edit",
    f"git -C {RM_WT} commit -m x --amend", f"git -C {RM_WT} switch -C master",
    f"git -C {RM_WT} switch --discard-changes master", f"git -C {RM_REPO} checkout master",
    f"git -C {RM_REPO} merge --ff-only feat/x", f"git -C {RM_REPO} commit -m x",
    "rm -rf Assets", "rm -fr /tmp/x", "rm -r Library", "TIMEOUT=1 git push",
    "ls && git push", f"git -C {RM_WT} status && git -C {RM_WT} reset --hard",
    # gh is read-only (#926): any method or body flag on `gh api` is a write
    "gh api -X DELETE repos/o/r", "gh api repos/o/r -X DELETE", "gh api repos/o/r -XPATCH",
    "gh api repos/o/r/issues --method POST", "gh api repos/o/r/issues --method=POST",
    "gh api repos/o/r/issues -f title=x", "gh api repos/o/r/labels -F name=x",
    "gh api repos/o/r/issues --field title=x", "gh api repos/o/r/issues --raw-field title=x",
    "gh api repos/o/r/issues --input body.json",
]
ALLOWED = [
    "git status", "git diff --stat", "git log --oneline -5", "git rev-parse HEAD",
    "git branch --show-current", f"git -C {RM_WT} status", f"git -C {RM_WT} add Assets/x.cs",
    f"git -C {RM_WT} commit -m 'feat(#833): x'", f"git -C {RM_WT} merge master",
    f"git -C {RM_WT} switch feat/833-x", f"git -C {RM_REPO} log --oneline -3",
    "ls -la", "grep -rn Foo Assets", "cd Assets", "cd Assets && ls",
    f"{STATE}/bin/runematch-test-PlayMode", f"{STATE}/bin/runematch-test-EditMode BLK6",
    f"{STATE}/bin/runematch-gate-1", f"{STATE}/bin/runematch-post-test-1",
    "gh search repos qwen-mm-plugins --limit 10", "gh repo view QwenLM/Qwen-MM-Plugins",
    "gh api repos/QwenLM/Qwen-MM-Plugins --jq .fork", "gh api orgs/QwenLM",
    "gh api users/octocat", "gh api search/repositories?q=x", "gh pr view 12",
    "gh pr diff 12", "gh issue list --limit 5", "gh release view v1 --repo o/r",
    "gh run view 123 --log",
]
REFUSED = [   # not denied, but no allow rule: dontAsk refuses them
    "git add x", "git commit -m x", "git merge master", "git checkout -- Assets",
    "scripts/unity.sh test EditMode", "flock /tmp/runematch-playmode.lock scripts/unity.sh x",
    f"{STATE}/bin/runematch-gate-1 extra", "python3 -c 'print(1)'", "curl https://x",
    f"git -C /mnt/c/Users/josia/Projects/RuneMatch-wt/lane-A commit -m x", "sed -i s/a/b/ x",
    "gh pr merge 12", "gh pr create", "gh pr comment 12 -b x", "gh issue create",
    "gh repo clone o/r", "gh repo delete o/r", "gh release create v1", "gh auth token",
    "gh secret list", "gh api graphql", "gh api user", "gh workflow run x",
    "gh repo edit o/r", "gh run rerun 1",
]


@pytest.mark.parametrize("cmd", DENIED)
def test_dangerous_commands_are_denied(rm_settings, cmd):
    assert P.decide_bash(rm_settings, cmd) == "deny"


@pytest.mark.parametrize("cmd", ALLOWED)
def test_worker_commands_are_allowed(rm_settings, cmd):
    assert P.decide_bash(rm_settings, cmd) == "allow"


@pytest.mark.parametrize("cmd", REFUSED)
def test_everything_else_is_refused(rm_settings, cmd):
    assert P.decide_bash(rm_settings, cmd) == "refuse"


@pytest.mark.parametrize("path,want", [
    (f"{RM_WT}/Assets/Scripts/Foo.cs", "allow"),
    (f"{RM_WT}/docs/DECISIONS.md", "allow"),
    (f"{RM_WT}/AGENTS.md", "deny"), (f"{RM_WT}/CLAUDE.md", "deny"),
    (f"{RM_WT}/AUTONOMY.md", "deny"), (f"{RM_WT}/sub/dir/CLAUDE.md", "deny"),
    (f"{RM_WT}/.claude/settings.json", "deny"), (f"{RM_WT}/.mcp.json", "deny"),
    (f"{RM_WT}/Assets/Scenes/Main.unity", "deny"), (f"{RM_WT}/Assets/P/X.prefab", "deny"),
    (f"{RM_REPO}/Assets/Scripts/Foo.cs", "deny"), (f"{RM_REPO}/CLAUDE.md", "deny"),
    (os.path.expanduser("~/.claude/settings.json"), "deny"),
    (os.path.expanduser("~/nomad/projects.yaml"), "deny"),
    (f"{STATE}/settings/runematch-833.json", "deny"), (f"{STATE}/bin/runematch-gate-1", "deny"),
    ("/tmp/runematch-playmode.lock", "refuse"), ("/etc/passwd", "refuse"),
])
def test_edits(rm_settings, path, want):
    assert P.decide_edit(rm_settings, path, cwd=RM_WT) == want


# ---- narrowing only --------------------------------------------------------------------------

BASE = """\
test: {run: "scripts/t.sh {filter}"}
"""


@pytest.mark.parametrize("block,match", [
    ('test: git push origin master\n', "denies"),
    ('test: scripts/t.sh\npost_test: ["git stash"]\n', "denies"),
    ('test: scripts/t.sh\npost_test: ["git -C . reset --hard"]\n', "denies"),
    ('test: scripts/t.sh\npost_test: ["git -c core.x=y clean -fdx"]\n', "denies"),
    ('test: "scripts/t.sh && rm -rf /"\n', "denies"),
    ('test: "FOO=1 git rebase master"\n', "denies"),
    ('test: "timeout 5 git push"\n', "denies"),
    ('build: "bash -c \'git push\'"\ntest: scripts/t.sh\n', "hides"),
    ('test: "sh -c make"\n', "hides"),
    ('test: "echo x | xargs git push"\n', "hides"),
    ('test: "scripts/t.sh \'{filter}\'"\n', "word of its own"),
    ('test: scripts/t.sh\nworking_dirs: ["~"]\n', "home folder"),
    ('test: scripts/t.sh\nworking_dirs: ["/"]\n', "absolute path"),
    ('test: scripts/t.sh\nworking_dirs: ["~/.claude/agents"]\n', "agent config"),
    ('test: scripts/t.sh\nworking_dirs: ["~/.local"]\n', "agent config"),
    ('test: scripts/t.sh\nworking_dirs: ["/srv/My Files"]\n', "absolute path"),
    ('test: scripts/t.sh\ndeny_edit_globs: ["!**/*.cs"]\n', "plain glob"),
    ('test: scripts/t.sh\ndeny_edit_globs: ["x) Bash(*"]\n', "plain glob"),
    ('test: {"bad suite": scripts/t.sh}\n', "suite name"),
    ('test: {run: scripts/t.sh, env: {"A-B": 1}}\n', "variable name"),
])
def test_a_project_entry_that_widens_is_rejected(tmp_path, block, match):
    p = _project(tmp_path, block)
    with pytest.raises(ConfigError, match=match):
        P.build_settings(p, str(tmp_path / "p-wt" / "ap-1"), state_dir=str(tmp_path / "st"))


def test_a_narrowing_entry_is_accepted_and_only_adds_wrappers_and_denies(tmp_path):
    p = _project(tmp_path, BASE + 'deny_edit_globs: ["**/*.lock"]\nworking_dirs: [../shared]\n'
                 'post_test: ["git checkout -- out/"]\n')
    (tmp_path / "shared").mkdir()
    wt, st = str(tmp_path / "p-wt" / "ap-1"), str(tmp_path / "st")
    s = P.build_settings(p, wt, state_dir=st)
    t = P.render_template(wt, p.repo, st)["permissions"]
    extra_allow = [r for r in s["permissions"]["allow"] if r not in t["allow"]]
    assert extra_allow == [f"Bash({st}/bin/p-test-default)", f"Bash({st}/bin/p-test-default *)",
                           f"Bash({st}/bin/p-post-test-1)"]
    assert set(t["deny"]) <= set(s["permissions"]["deny"])
    assert "Edit(**/*.lock)" in s["permissions"]["deny"]
    assert f"Edit(/{tmp_path}/shared/**)" in s["permissions"]["deny"]    # read-only
    assert s["permissions"]["additionalDirectories"] == [wt, p.repo, str(tmp_path / "shared")]


def test_check_narrowing_catches_a_tampered_result(tmp_path):
    p = _project(tmp_path, BASE)
    wt, st = str(tmp_path / "p-wt" / "ap-1"), str(tmp_path / "st")
    ws = P.wrappers(p, st)
    t = P.render_template(wt, p.repo, st)
    for mutate, match in [
        (lambda s: s["permissions"]["allow"].append("Bash(git -C:*)"), "beyond the template"),
        (lambda s: s["permissions"]["deny"].remove("Bash(git push *)"), "dropped"),
        (lambda s: s["permissions"].update(defaultMode="bypassPermissions"), "defaultMode"),
        (lambda s: s.update(hooks={}), "unexpected settings key"),
    ]:
        s = P.build_settings(p, wt, state_dir=st)
        mutate(s)
        with pytest.raises(ConfigError, match=match):
            P.check_narrowing(s, t, ws)


def test_worktree_must_be_under_worktree_root_and_outside_the_repo(tmp_path):
    p = _project(tmp_path, BASE)
    with pytest.raises(ConfigError, match="worktree_root"):
        P.build_settings(p, str(tmp_path / "elsewhere"), state_dir=str(tmp_path / "st"))
    p2 = _project(tmp_path, BASE + "worktree_root: .wt\n", pid="q")
    with pytest.raises(ConfigError, match="inside the main checkout"):
        P.build_settings(p2, str(tmp_path / "q" / ".wt" / "ap-1"), state_dir=str(tmp_path / "st"))


# ---- wrappers ----------------------------------------------------------------------------------

FAKE_TOOL = """\
#!/bin/sh
printf 'argc=%s\\n' "$#"
for a in "$@"; do printf 'arg=[%s]\\n' "$a"; done
printf 'env=[%s]\\n' "${WEIRD-unset}"
printf 'pwd=%s\\n' "$(pwd)"
if flock -n /LOCK true 2>/dev/null; then echo lock=free; else echo lock=held; fi
"""


@pytest.fixture
def wrapped(tmp_path):
    """A project whose test command holds a lock and has an awkward env value, a real worktree
    with a fake test tool, and the wrapper written to disk."""
    lock = str(tmp_path / "locks" / "t.lock")
    os.makedirs(os.path.dirname(lock))
    block = f"""\
        test:
          run: "scripts/tool.sh go {{filter}}"
          locks: [{lock}]
          env: {{WEIRD: "a 'b' \\"c\\" $HOME `x` ;d"}}
        locks: [{lock}]
        """
    p = _project(tmp_path, block)
    wt = tmp_path / "p-wt" / "ap-1"
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    (wt / "scripts").mkdir()
    tool = wt / "scripts" / "tool.sh"
    tool.write_text(FAKE_TOOL.replace("/LOCK", lock))
    tool.chmod(0o755)
    (wt / "sub").mkdir()
    st = str(tmp_path / "st")
    path, ws = P.write(p, str(wt), state_dir=st, task_id=1)
    return {"wrapper": ws[0].path, "wt": str(wt), "lock": lock, "settings": path, "tmp": tmp_path}


def _run(w, *args, cwd=None):
    r = subprocess.run([w["wrapper"], *args], cwd=cwd or os.path.join(w["wt"], "sub"),
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def test_wrapper_runs_from_the_worktree_top_with_env_and_lock(wrapped):
    code, out, err = _run(wrapped)
    assert code == 0, err
    assert "argc=1\narg=[go]\n" in out                      # no filter: the placeholder drops
    assert """env=[a 'b' "c" $HOME `x` ;d]""" in out         # env value passed literally
    assert f"pwd={os.path.realpath(wrapped['wt'])}" in out
    assert "lock=held" in out


def test_wrapper_passes_a_valid_filter_as_one_argument(wrapped):
    code, out, _ = _run(wrapped, "RuneMerge.Tests.BlockerTests.BLK6_One,PWR3")
    assert code == 0
    assert "argc=2\narg=[go]\narg=[RuneMerge.Tests.BlockerTests.BLK6_One,PWR3]\n" in out


@pytest.mark.parametrize("bad", ["a b", "a;touch /tmp/pwned", "$(id)", "`id`", "-x", "--help",
                                 "a|b", "a'b", 'a"b', "*", "a\nb", "", "x" * 201, "a&b", "a>b"])
def test_wrapper_rejects_a_bad_filter_before_running_anything(wrapped, bad):
    code, out, err = _run(wrapped, bad)
    assert code == 64 and out == "" and "filter" in err
    assert P.validate_filter(bad) is False


def test_python_and_shell_filter_rules_agree(wrapped):
    for f in ["BLK6", "A.B.C", "a,b", "x:y=z+1", "Tests/Edit", "a-b", "_x", "x" * 200]:
        assert P.validate_filter(f), f
        assert _run(wrapped, f)[0] == 0, f


def test_wrapper_takes_one_filter_at_most(wrapped):
    code, _, err = _run(wrapped, "a", "b")
    assert code == 64 and "at most one" in err


def test_wrapper_refuses_outside_the_worktree_root(wrapped, tmp_path):
    other = tmp_path / "p"                                  # the main checkout
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    code, out, err = _run(wrapped, cwd=str(other))
    assert code == 65 and out == ""


def test_wrapper_waits_for_the_lock(wrapped):
    holder = subprocess.Popen(["flock", wrapped["lock"], "sleep", "1.5"])
    try:
        import time
        time.sleep(0.2)
        t0 = time.monotonic()
        assert _run(wrapped)[0] == 0
        assert time.monotonic() - t0 > 1.0
    finally:
        holder.wait()


def test_written_files_are_read_only_scripts_and_json(wrapped):
    assert oct(os.stat(wrapped["wrapper"]).st_mode & 0o777) == "0o555"
    with open(wrapped["settings"]) as f:
        s = json.load(f)
    assert s["permissions"]["defaultMode"] == "dontAsk"
    assert wrapped["settings"].endswith("/settings/p-1.json")


def test_gpu_projects_take_the_gpu_lock_for_tests_and_build(tmp_path):
    p = _project(tmp_path, "test: scripts/t.sh\nbuild: make\npost_test: [scripts/clean.sh]\n"
                 "requires_gpu: true\n")
    ws = {w.name: w.script for w in P.wrappers(p, str(tmp_path / "st"))}
    assert P.GPU_LOCK in ws["p-test-default"] and P.GPU_LOCK in ws["p-build"]
    assert P.GPU_LOCK not in ws["p-post-test-1"]


def test_cli_args_keep_other_settings_sources_out():
    assert P.cli_args("/st/settings/s.json") == [
        "--settings", "/st/settings/s.json", "--setting-sources", "",
        "--mcp-config", "/st/settings/mcp.json", "--strict-mcp-config",
        "--permission-mode", "dontAsk"]


def test_write_puts_only_nomad_pm_next_to_the_settings(tmp_path, mcp_source):
    p = _project(tmp_path, "test: scripts/t.sh\n")
    path, _ = P.write(p, str(tmp_path / "p-wt" / "ap-7"), state_dir=str(tmp_path / "st"), task_id=7)
    cfg = json.load(open(P.mcp_path(path)))
    assert cfg == {"mcpServers": {"nomad-pm": json.loads(mcp_source.read_text())
                                  ["mcpServers"]["nomad-pm"]}}      # penpot stays out
    crit = P.write_criteria(str(tmp_path / "st"))
    assert P.mcp_path(crit) == P.mcp_path(path)


def test_mcp_config_follows_the_operator_config(tmp_path, mcp_source):
    moved = {"type": "stdio", "command": "/opt/pm", "args": ["x"]}
    mcp_source.write_text(json.dumps({"mcpServers": {"nomad-pm": moved}}))
    path = P.write_criteria(str(tmp_path / "st"))
    assert json.load(open(P.mcp_path(path)))["mcpServers"]["nomad-pm"] == moved


@pytest.mark.parametrize("content", ['{"mcpServers": {"penpot": {}}}', "{}", "not json", None])
def test_no_nomad_pm_means_no_run(tmp_path, mcp_source, content):
    if content is None:
        mcp_source.unlink()
    else:
        mcp_source.write_text(content)
    p = _project(tmp_path, "test: scripts/t.sh\n")
    with pytest.raises(ConfigError, match="nomad-pm|can't read"):
        P.write(p, str(tmp_path / "p-wt" / "ap-7"), state_dir=str(tmp_path / "st"), task_id=7)
    with pytest.raises(ConfigError):
        P.write_criteria(str(tmp_path / "st"))


# ---- default branch and the shared rules (#989) ------------------------------------------------

# The template lines #989 adds. A worker can't write the template (the nomad entry's
# deny_edit_globs), so until the operator applies them the real-template tests below are xfail.
RULES_989 = {
    "deny": [
        "Bash(git switch {default_branch} *)",
        "Bash(git switch * {default_branch} *)",
        "Bash(git checkout {default_branch} *)",
        "Bash(git checkout * {default_branch} *)",
        "Bash(git -C {worktree} switch {default_branch} *)",
        "Bash(git -C {worktree} switch * {default_branch} *)",
        "Bash(git -C {worktree} checkout {default_branch} *)",
        "Bash(git -C {worktree} checkout * {default_branch} *)",
        "Edit(**/AUTONOMY*.md)",
        "Edit(//**/AUTONOMY*.md)",
    ],
}


def _template_989():
    t = P.load_template()
    t["permissions"]["deny"] = list(dict.fromkeys(t["permissions"]["deny"] + RULES_989["deny"]))
    return t


def test_protected_branches_are_main_master_and_the_default(tmp_path):
    assert P.protected_branches(str(tmp_path / "nope")) == ["main", "master"]   # no repo
    assert P.protected_branches(str(tmp_path), "trunk") == ["main", "master", "trunk"]
    assert P.protected_branches(str(tmp_path), "master") == ["main", "master"]
    for bad in ("a b", "x)", "*", "-x", "a..b"):
        with pytest.raises(ConfigError, match="default branch"):
            P.protected_branches(str(tmp_path), bad)


def test_protected_branches_looks_the_default_up_in_the_repo(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/trunk", "HEAD"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD",
                    "refs/remotes/origin/trunk"], check=True)
    assert P.protected_branches(str(repo)) == ["main", "master", "trunk"]


def test_a_default_branch_rule_renders_once_per_protected_branch(tmp_path):
    wt = str(tmp_path / "wt")
    t = {"permissions": {"deny": ["Bash(git -C {worktree} switch {default_branch} *)",
                                  "Bash(git push *)"]}}
    d = P.render_template(wt, str(tmp_path), "/st", t, ["main", "master", "trunk"])
    assert d["permissions"]["deny"] == [f"Bash(git -C {wt} switch main *)",
                                        f"Bash(git -C {wt} switch master *)",
                                        f"Bash(git -C {wt} switch trunk *)", "Bash(git push *)"]


def _settings_989(tmp_path, template):
    p = _project(tmp_path, BASE)
    wt = str(tmp_path / "p-wt" / "ap-1")
    s = P.build_settings(p, wt, state_dir=str(tmp_path / "st"), template=template,
                         default_branch="trunk")
    return s, wt


SWITCH_DENIED = [
    "git -C {wt} switch trunk", "git -C {wt} switch master", "git -C {wt} switch main",
    "git -C {wt} switch --detach trunk", "git -C {wt} switch -m master",
    "git -C {wt} checkout trunk", "git -C {wt} checkout -q master", "git switch trunk",
    "git checkout master", "git -C {wt} status && git -C {wt} switch trunk",
]
SWITCH_ALLOWED = ["git -C {wt} switch feat/1-x", "git -C {wt} switch feat/1-master-fix",
                  "git -C {wt} merge trunk"]


def _check_switch(s, wt):
    for c in SWITCH_DENIED:
        assert P.decide_bash(s, c.format(wt=wt)) == "deny", c
    for c in SWITCH_ALLOWED:
        assert P.decide_bash(s, c.format(wt=wt)) == "allow", c


def _check_autonomy_edits(s, wt):
    deny = s["permissions"]["deny"]
    assert "Edit(**/AUTONOMY*.md)" in deny and "Edit(//**/AUTONOMY*.md)" in deny
    for rule, cwd in [("Edit(**/AUTONOMY*.md)", wt), ("Edit(//**/AUTONOMY*.md)", "/elsewhere")]:
        one = {"permissions": {"deny": [rule]}}       # each form on its own
        for f in ("autopilot/AUTONOMY.template.md", "AUTONOMY.md", "docs/AUTONOMY.md"):
            assert P.decide_edit(one, f"{wt}/{f}", cwd=cwd) == "deny", (rule, f)
    for f in ("autopilot/AUTONOMY.template.md", "AUTONOMY.md"):
        assert P.decide_edit(s, f"{wt}/{f}", cwd=wt) == "deny", f
    assert P.decide_edit(s, f"{wt}/autopilot/worker.py", cwd=wt) == "allow"


def test_rules_989_refuse_switching_to_the_default_branch(tmp_path):
    _check_switch(*_settings_989(tmp_path, _template_989()))


def test_rules_989_refuse_edits_to_the_shared_rules(tmp_path):
    _check_autonomy_edits(*_settings_989(tmp_path, _template_989()))


def test_the_template_refuses_switching_to_the_default_branch(tmp_path):
    _check_switch(*_settings_989(tmp_path, None))


def test_the_template_refuses_edits_to_the_shared_rules(tmp_path):
    _check_autonomy_edits(*_settings_989(tmp_path, None))


# ---- the real claude CLI (opt-in) ---------------------------------------------------------------

CLAUDE_CASES = {   # command -> must it be refused (denied or not allowed)?
    "git -C {wt} status": False,
    "git -C {wt} push origin HEAD": True,
    "git push origin HEAD": True,
    "git stash list": True,
    "git -C {wt} reset --hard": True,
    "git -C {wt} commit --amend -m x": True,     # matches the commit allow; the deny wins
    "{wrapper}": False,
    "{wrapper} BLK6": False,
    "TIMEOUT=5 flock {lock} scripts/tool.sh go": True,
    "echo hi > {wt}/CLAUDE.md": True,
    "cat {wt}/README": False,
    # allowed only by the operator's ~/.claude/settings.json, which cli_args keeps out:
    "git -C {tmp} status": True,
    "docker compose version": True,
}
CLAUDE_WRITES = {  # file -> must the Write tool be refused?
    "{wt}/notes.txt": False,
    "{wt}/AGENTS.md": True,
    "{wt}/.claude/settings.json": True,
    "{repo}/stray.txt": True,
}


@pytest.mark.skipif(os.environ.get("AUTOPILOT_CLAUDE_E2E") != "1" or not shutil.which("claude"),
                    reason="set AUTOPILOT_CLAUDE_E2E=1 to check with the real claude CLI")
def test_claude_cli_enforces_the_profile(wrapped):
    """One headless haiku run under the generated settings. The model is told to run each
    command once; the verdicts come from Claude Code's own `permission_denials`."""
    w = wrapped
    (open(os.path.join(w["wt"], "README"), "w")).write("x\n")
    v = dict(wt=w["wt"], wrapper=w["wrapper"], lock=w["lock"], tmp=str(w["tmp"]),
             repo=str(w["tmp"] / "p"))
    cmds = [c.format(**v) for c in CLAUDE_CASES]
    files = [f.format(**v) for f in CLAUDE_WRITES]
    prompt = ("This is a permissions test. Use the Bash tool to run each of these commands "
              "exactly as written, one per Bash call, in order, even if one fails or is refused. "
              "Do not retry or change any command, and run nothing else.\n\n"
              + "\n".join(cmds)
              + "\n\nThen use the Write tool (not Bash) to write the text x into each of these "
              "files, one Write call per file, even if one is refused:\n\n" + "\n".join(files)
              + "\n\nThen reply DONE.")
    r = subprocess.run(["claude", "-p", prompt, "--model", "haiku", "--output-format", "json",
                        "--max-turns", str(len(cmds) + len(files) + 3), *P.cli_args(w["settings"])],
                       cwd=w["wt"], capture_output=True, text=True, timeout=600)
    out = json.loads(r.stdout)
    if os.environ.get("AUTOPILOT_CLAUDE_E2E_DUMP"):
        print(json.dumps({k: out.get(k) for k in ("result", "permission_denials", "num_turns")}, indent=1))
    ds = out.get("permission_denials", [])
    denied = {d.get("tool_input", {}).get("command") for d in ds}
    refused_files = {d.get("tool_input", {}).get("file_path") for d in ds}
    for tmpl, c in zip(CLAUDE_CASES, cmds):
        assert (c in denied) == CLAUDE_CASES[tmpl], (c, sorted(denied))
    for tmpl, f in zip(CLAUDE_WRITES, files):
        assert (f in refused_files) == CLAUDE_WRITES[tmpl], (f, sorted(refused_files))
        assert os.path.exists(f) != CLAUDE_WRITES[tmpl], f
    assert not os.path.exists(os.path.join(w["wt"], "CLAUDE.md"))


# ---- no-grant golden files (#979, PRD ap-permission-grants 5.7) --------------------------------

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "testdata", "golden")
GOLDEN_HOME = "/home/op"        # entry paths resolve here, so the files hold no machine paths


def _golden_projects():
    return [p for p in projects.load(REAL, base=GOLDEN_HOME) if p.autopilot]


def _golden_text(p):
    wt = os.path.join(p.autopilot.worktree_root, "ap-1")
    s = P.build_settings(p, wt, state_dir=STATE, default_branch="master")
    return json.dumps(s, indent=2) + "\n"


@pytest.mark.parametrize("pid", [p.id for p in _golden_projects()])
def test_no_grant_profile_is_byte_identical_to_the_golden_file(pid):
    """Rendered at the commit before #979. A missing file is written and the test fails, so a
    new golden is always reviewed and committed, never taken silently."""
    p = next(p for p in _golden_projects() if p.id == pid)
    path = os.path.join(GOLDEN_DIR, f"{pid}-settings.json")
    text = _golden_text(p)
    if not os.path.exists(path):
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        pytest.fail(f"golden file {path} written; review and commit it")
    with open(path) as f:
        assert text == f.read()


# ---- snapshot regeneration ----------------------------------------------------------------------

if __name__ == "__main__" and sys.argv[1:] == ["--snapshot"]:
    s = P.build_settings(_runematch(), RM_WT, state_dir=STATE)
    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    with open(SNAPSHOT, "w") as f:
        json.dump(s, f, indent=2)
        f.write("\n")
    print("wrote", SNAPSHOT)
