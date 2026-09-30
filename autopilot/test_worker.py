"""Tests for autopilot workers (#866): worktrees, the `claude -p` launch, stream-json parsing,
time limits, kill vs finish. A fake `claude` script emits canned stream-json; git runs for real
in a temp repo.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot -q
"""
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import projects  # noqa: E402
import worker as wk  # noqa: E402
from projects import Autopilot, Command, Project  # noqa: E402

FAKE = r'''#!{python}
import json, os, sys, time
scenario = os.environ.get("FAKE_SCENARIO", "success")
with open(os.environ["FAKE_ARGV"], "w") as f:
    json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd(),
               "api_key": os.environ.get("ANTHROPIC_API_KEY")}}, f)

def emit(o):
    print(json.dumps(o), flush=True)

def bash(i, cmd):
    emit({{"type": "assistant", "message": {{"content": [
        {{"type": "tool_use", "id": i, "name": "Bash", "input": {{"command": cmd}}}}]}}}})

def res(i, text, err=False):
    emit({{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "tool_use_id": i, "is_error": err, "content": text}}]}}}})

DENIED = "Permission to use Bash has been denied because Claude Code is running in don't ask mode."
emit({{"type": "system", "subtype": "init", "cwd": os.getcwd()}})
if scenario == "timeout":
    time.sleep(600)
denials, text = [], "Built it.\n\nautopilot: result ready to merge"
if scenario == "success":
    bash("t1", "git status"); res("t1", "clean")
elif scenario == "nomarker":
    bash("t1", "git status"); res("t1", "clean"); text = "I did some things."
elif scenario == "refused":
    bash("t1", "git status && ls"); res("t1", DENIED, True)
    denials = [{{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {{"command": "git status && ls"}}}}]
    text = "Bash is denied in this session, so I stopped.\n\nautopilot: result failed"
elif scenario == "recovered":
    bash("t1", "git status && ls"); res("t1", DENIED, True)
    bash("t2", "git status"); res("t2", "clean")
    denials = [{{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {{"command": "git status && ls"}}}}]
elif scenario == "crash":
    sys.stderr.write("boom: auth failed\n"); sys.exit(2)
emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": text}}]}}}})
emit({{"type": "result", "subtype": "success", "is_error": False, "result": text,
      "total_cost_usd": 0.42, "permission_denials": denials}})
'''


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    sh(r, "git", "init", "-q", "-b", "main")
    sh(r, "git", "config", "user.email", "t@example.com")
    sh(r, "git", "config", "user.name", "t")
    (r / "README").write_text("hi\n")
    sh(r, "git", "add", ".")
    sh(r, "git", "commit", "-qm", "init")
    sh(r, "git", "switch", "-qc", "feat/other")   # main checkout is not on the default branch
    return str(r)


@pytest.fixture
def project(repo, tmp_path):
    ap = Autopilot(test={"default": Command("pytest -q {filter}")},
                   pre_merge_gate=(Command("pytest -q {filter}"),), milestone_gate=(),
                   locks=("/tmp/x-test.lock",), worktree_root=str(tmp_path / "proj-wt"),
                   deny_edit_globs=("**/*.prefab",))
    return Project(id="proj", path=repo, autopilot=ap)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = tmp_path / "claude"
    f.write_text(FAKE.format(python=sys.executable))
    f.chmod(0o755)
    monkeypatch.setenv("FAKE_ARGV", str(tmp_path / "argv.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-blanked")
    return str(f)


class Ctx:
    def __init__(self, pid):
        self.project_id = pid
        self.events = []

    def event(self, kind, msg):
        self.events.append((kind, msg))


def pool_for(project, fake, tmp_path, **kw):
    return wk.WorkerPool(project_of=lambda pid: project if pid == project.id else None,
                         claude=fake, state_dir=str(tmp_path / "state"), **kw)


def wait_done(pool, ctx, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        out = pool.collect(ctx)
        if out:
            return out
        time.sleep(0.05)
    raise AssertionError("worker never finished")


# ---- worktrees --------------------------------------------------------------------------------

def test_new_worktree_on_a_task_branch_from_the_default_branch(project, tmp_path):
    wt = wk.ensure_worktree(project.repo, project.autopilot.worktree_root, "42", "Fix the Thing!")
    assert wt.path == str(tmp_path / "proj-wt" / "ap-42")
    assert wt.branch == "feat/42-fix-the-thing" and wt.base == "main"
    assert wt.created and wt.new_branch and not wt.dirty
    assert sh(wt.path, "git", "branch", "--show-current") == wt.branch
    assert sh(wt.path, "git", "rev-parse", "HEAD") == sh(project.repo, "git", "rev-parse", "main")
    assert sh(project.repo, "git", "branch", "--show-current") == "feat/other"   # untouched


def test_an_existing_task_branch_is_reused(project):
    sh(project.repo, "git", "branch", "feat/7-old-slug", "main")
    sh(project.repo, "git", "branch", "feat/77-not-this", "main")
    wt = wk.ensure_worktree(project.repo, project.autopilot.worktree_root, "7", "New title")
    assert wt.branch == "feat/7-old-slug" and wt.created and not wt.new_branch


def test_a_dirty_worktree_is_kept_and_reported(project):
    root = project.autopilot.worktree_root
    wt = wk.ensure_worktree(project.repo, root, "9", "x")
    with open(os.path.join(wt.path, "half.txt"), "w") as f:
        f.write("work in progress\n")
    again = wk.ensure_worktree(project.repo, root, "9", "x")
    assert again.path == wt.path and again.branch == wt.branch
    assert again.dirty and not again.created
    assert open(os.path.join(wt.path, "half.txt")).read() == "work in progress\n"


def test_a_branch_held_by_another_clean_worktree_is_adopted(project, tmp_path):
    lane = str(tmp_path / "lane-A")
    sh(project.repo, "git", "worktree", "add", "-b", "feat/833-guides-idle", lane, "main")
    wt = wk.ensure_worktree(project.repo, project.autopilot.worktree_root, "833", "Guides idle")
    assert os.path.realpath(wt.path) == os.path.realpath(lane)
    assert wt.branch == "feat/833-guides-idle"
    assert not wt.created and not wt.new_branch and not wt.dirty
    assert not os.path.exists(os.path.join(project.autopilot.worktree_root, "ap-833"))


def test_a_branch_held_by_a_dirty_worktree_is_refused_and_left_alone(project, tmp_path):
    lane = str(tmp_path / "lane-B")
    sh(project.repo, "git", "worktree", "add", "-b", "feat/5-busy", lane, "main")
    with open(os.path.join(lane, "wip.txt"), "w") as f:
        f.write("someone is working here\n")
    with pytest.raises(wk.WorkerError, match="uncommitted changes"):
        wk.ensure_worktree(project.repo, project.autopilot.worktree_root, "5", "busy")
    assert open(os.path.join(lane, "wip.txt")).read() == "someone is working here\n"


def test_regenerated_paths_are_reset_so_a_held_worktree_is_adopted(project, tmp_path):
    sh(project.repo, "git", "switch", "-q", "main")
    os.makedirs(os.path.join(project.repo, "Fonts & Stuff"))
    with open(os.path.join(project.repo, "Fonts & Stuff", "atlas.asset"), "w") as f:
        f.write("v1\n")
    sh(project.repo, "git", "add", ".")
    sh(project.repo, "git", "commit", "-qm", "atlas")
    sh(project.repo, "git", "switch", "-q", "feat/other")
    lane = str(tmp_path / "lane-F")
    sh(project.repo, "git", "worktree", "add", "-b", "feat/647-capture", lane, "main")
    with open(os.path.join(lane, "Fonts & Stuff", "atlas.asset"), "w") as f:
        f.write("glyph cache\n")
    sh(lane, "git", "add", ".")                   # staged changes are reset too
    wt = wk.ensure_worktree(project.repo, project.autopilot.worktree_root, "647", "capture",
                            regenerated=("Fonts & Stuff/atlas.asset",))
    assert os.path.realpath(wt.path) == os.path.realpath(lane) and not wt.dirty
    assert open(os.path.join(lane, "Fonts & Stuff", "atlas.asset")).read() == "v1\n"
    assert sh(lane, "git", "status", "--porcelain") == ""


def test_regenerated_paths_never_hide_other_changes(project, tmp_path):
    lane = str(tmp_path / "lane-G")
    sh(project.repo, "git", "worktree", "add", "-b", "feat/648-busy", lane, "main")
    with open(os.path.join(lane, "README"), "w") as f:
        f.write("real work\n")
    with pytest.raises(wk.WorkerError, match="uncommitted changes"):
        wk.ensure_worktree(project.repo, project.autopilot.worktree_root, "648", "busy",
                           regenerated=("atlas.asset",))
    assert open(os.path.join(lane, "README")).read() == "real work\n"


def test_a_branch_in_the_main_checkout_is_refused(project):
    sh(project.repo, "git", "branch", "-m", "feat/other", "feat/6-main-here")
    with pytest.raises(wk.WorkerError, match="main checkout"):
        wk.ensure_worktree(project.repo, project.autopilot.worktree_root, "6", "x")


def test_a_non_repo_is_refused(tmp_path):
    with pytest.raises(wk.WorkerError):
        wk.ensure_worktree(str(tmp_path), str(tmp_path / "wt"), "1", "x")


# ---- the run ----------------------------------------------------------------------------------

def test_launch_invocation_log_and_success(project, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    pool = pool_for(project, fake, tmp_path)
    ctx = Ctx("proj")
    started = pool.start_workers(ctx, [{"task": "5", "title": "Add a widget"}])
    assert [w["task"] for w in started] == ["5"]
    assert started[0]["branch"] == "feat/5-add-a-widget"
    json.dumps(started)                                    # the supervisor stores it as JSON
    [out] = wait_done(pool, ctx)
    assert out["task"] == "5" and out["outcome"] == "merge" and out["cost_usd"] == 0.42
    assert out["denials"] == [] and out["detail"] == ""

    seen = json.load(open(tmp_path / "argv.json"))
    argv = seen["argv"]
    assert argv[0] == "-p"
    i = argv.index("--settings")
    # the #875 profile: user/project/local settings kept out, so no operator allow merges in
    mcp = os.path.join(os.path.dirname(argv[i + 1]), "mcp.json")
    assert argv[i + 2:] == ["--setting-sources", "", "--mcp-config", mcp, "--strict-mcp-config",
                            "--permission-mode", "dontAsk",
                            "--verbose", "--output-format", "stream-json"]
    # the run gets nomad-pm, and only nomad-pm (#832/#842 ran with no tracker at all)
    assert list(json.load(open(mcp))["mcpServers"]) == ["nomad-pm"]
    assert seen["cwd"] == started[0]["worktree"] and seen["api_key"] == ""
    prompt = argv[1]
    assert "#5" in prompt and started[0]["worktree"] in prompt and "feat/5-add-a-widget" in prompt
    assert "pytest -q {filter}" in prompt and "AUTONOMY" in prompt
    wrapper = str(tmp_path / "state" / "bin" / "proj-test-default")
    assert f"`{wrapper} [filter]`" in prompt and "ONLY through its wrapper" in prompt
    assert "promote" not in prompt                            # not a New task

    settings_file = argv[i + 1]
    assert settings_file == str(tmp_path / "state" / "settings" / "proj-5.json")
    settings = json.load(open(settings_file))["permissions"]
    assert started[0]["worktree"] in settings["additionalDirectories"]
    assert "/tmp" in settings["additionalDirectories"]           # the lock's directory
    assert any(d.startswith("Bash(git push") for d in settings["deny"])
    assert "Edit(**/*.prefab)" in settings["deny"]
    assert f"Bash({wrapper})" in settings["allow"] and f"Bash({wrapper} *)" in settings["allow"]
    assert not any(a.startswith("Bash(pytest") for a in settings["allow"])   # never the raw command
    assert os.access(wrapper, os.X_OK)

    log_dir = tmp_path / "state" / "runs" / "proj"
    assert out["log"].startswith(str(log_dir)) and out["log"].endswith(".jsonl")
    assert all(json.loads(line) for line in open(out["log"]))


def test_an_adopted_lane_gets_the_settings_for_its_own_path(project, fake, tmp_path, monkeypatch):
    # #944: the rules used to be rendered for ap-<id> while the run started in the lane, so every
    # edit and every `git -C <lane>` write was refused (RuneMatch #639, lane-B).
    lane = os.path.join(project.autopilot.worktree_root, "lane-B")
    sh(project.repo, "git", "worktree", "add", "-b", "feat/639-3d-interior", lane, "main")
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    pool = pool_for(project, fake, tmp_path)
    ctx = Ctx("proj")
    [started] = pool.start_workers(ctx, [{"task": "639", "title": "3D interiors"}])
    wait_done(pool, ctx)
    assert os.path.realpath(started["worktree"]) == os.path.realpath(lane)
    argv = json.load(open(tmp_path / "argv.json"))["argv"]
    raw = open(argv[argv.index("--settings") + 1]).read()
    perms = json.loads(raw)["permissions"]
    assert f"Edit(/{lane}/**)" in perms["allow"]
    assert f"Bash(git -C {lane} merge *)" in perms["allow"]
    assert lane in perms["additionalDirectories"]
    assert "ap-639" not in raw
    assert not os.path.exists(os.path.join(project.autopilot.worktree_root, "ap-639"))


def test_an_adopted_lane_outside_the_worktree_root_is_refused(project, fake, tmp_path,
                                                               monkeypatch):
    lane = str(tmp_path / "lane-X")
    sh(project.repo, "git", "worktree", "add", "-b", "feat/12-elsewhere", lane, "main")
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    pool = pool_for(project, fake, tmp_path)
    ctx = Ctx("proj")
    assert pool.start_workers(ctx, [{"task": "12"}]) == []
    assert any(k == "error" and "lane-X" in m and "worker profile" in m for k, m in ctx.events)
    assert not os.path.exists(tmp_path / "argv.json")                 # claude never started
    assert not os.path.exists(os.path.join(project.autopilot.worktree_root, "ap-12"))
    assert sh(lane, "git", "branch", "--show-current") == "feat/12-elsewhere"


def test_project_rules_are_named_when_present(project, fake, tmp_path, monkeypatch):
    open(os.path.join(project.repo, "AUTONOMY.md"), "w").write("# local\n")
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    pool = pool_for(project, fake, tmp_path)
    ctx = Ctx("proj")
    pool.start_workers(ctx, [{"task": "6"}])
    wait_done(pool, ctx)
    prompt = json.load(open(tmp_path / "argv.json"))["argv"][1]
    assert os.path.join(project.repo, "AUTONOMY.md") in prompt


def test_missing_marker_is_failed(project, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "nomarker")
    pool = pool_for(project, fake, tmp_path)
    ctx = Ctx("proj")
    pool.start_workers(ctx, [{"task": "5"}])
    [out] = wait_done(pool, ctx)
    assert out["outcome"] == "failed" and "marker" in out["detail"] and out["cost_usd"] == 0.42


def test_tracker_marker_is_used_when_the_message_has_none(project, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "nomarker")
    pool = pool_for(project, fake, tmp_path, tracker=lambda tid: "needs_operator")
    ctx = Ctx("proj")
    pool.start_workers(ctx, [{"task": "5"}])
    [out] = wait_done(pool, ctx)
    assert out["outcome"] == "needs_operator"


def test_first_shell_refusal_is_error_with_the_command(project, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "refused")
    pool = pool_for(project, fake, tmp_path)
    ctx = Ctx("proj")
    pool.start_workers(ctx, [{"task": "5"}])
    [out] = wait_done(pool, ctx)
    assert out["outcome"] == "error"
    assert "git status && ls" in out["detail"]
    assert out["denials"] == [{"tool": "Bash", "id": "t1", "command": "git status && ls"}]


def test_a_refusal_the_run_recovered_from_keeps_its_outcome(project, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "recovered")
    pool = pool_for(project, fake, tmp_path)
    ctx = Ctx("proj")
    pool.start_workers(ctx, [{"task": "5"}])
    [out] = wait_done(pool, ctx)
    assert out["outcome"] == "merge"
    assert "git status && ls" in out["detail"] and len(out["denials"]) == 1


def test_a_crash_is_failed_with_stderr(project, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "crash")
    pool = pool_for(project, fake, tmp_path)
    ctx = Ctx("proj")
    pool.start_workers(ctx, [{"task": "5"}])
    [out] = wait_done(pool, ctx)
    assert out["outcome"] == "failed" and "exit 2" in out["detail"] and "auth failed" in out["detail"]


def test_time_limit_kills_the_group_and_resumes(project, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "timeout")
    monkeypatch.setattr(wk, "KILL_GRACE_SECS", 2)
    pool = pool_for(project, fake, tmp_path, run_secs=0.5)
    ctx = Ctx("proj")
    [w] = pool.start_workers(ctx, [{"task": "5"}])
    assert pool.collect(ctx) == []                       # still running, before the deadline
    time.sleep(0.7)
    [out] = pool.collect(ctx)
    assert out["outcome"] == "resume" and "time limit" in out["detail"]
    assert not pool.live()
    with pytest.raises(ProcessLookupError):
        os.killpg(w["pid"], 0)                           # the whole group is gone
    assert sh(project.repo, "git", "rev-parse", "--verify", w["branch"])   # branch kept


def test_a_task_with_a_live_worker_is_not_started_twice(project, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "timeout")
    pool = pool_for(project, fake, tmp_path)
    ctx = Ctx("proj")
    pool.start_workers(ctx, [{"task": "5"}])
    assert pool.start_workers(ctx, [{"task": "5"}]) == []
    assert any("already has a live worker" in m for _, m in ctx.events)
    pool.stop(ctx, "kill")


def test_stop_kill_vs_finish(project, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "timeout")
    pool = pool_for(project, fake, tmp_path)
    ctx = Ctx("proj")
    [w] = pool.start_workers(ctx, [{"task": "5"}])

    pool.stop(ctx, "finish")
    time.sleep(0.2)
    assert len(pool.live("proj")) == 1                   # finish: it keeps running

    pool.stop(ctx, "kill")
    assert pool.live("proj") == []
    [out] = pool.collect(ctx)
    assert out["outcome"] == "resume" and "operator" in out["detail"]
    assert sh(project.repo, "git", "rev-parse", "--verify", w["branch"])   # branch kept
    assert os.path.isdir(w["worktree"])


def test_other_projects_runs_are_left_alone(project, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "timeout")
    pool = pool_for(project, fake, tmp_path)
    pool.start_workers(Ctx("proj"), [{"task": "5"}])
    pool.stop(Ctx("elsewhere"), "kill")
    assert pool.collect(Ctx("elsewhere")) == [] and len(pool.live("proj")) == 1
    pool.stop(Ctx("proj"), "kill")


# ---- parsing on its own -----------------------------------------------------------------------

@pytest.mark.parametrize("text, want", [
    ("autopilot: result ready to merge", "merge"),
    ("done.\n**autopilot: result needs operator**", "needs_operator"),
    ("Autopilot: Result: Resume", "resume"),
    ("autopilot: result failed\n...\nautopilot: result ready to merge", "merge"),
    ("autopilot: started feat/1-x", None),
    ("", None),
])
def test_find_marker(text, want):
    assert wk.find_marker(text) == want


def test_trial_log_shape_is_parsed(tmp_path):
    """The shape of a real RuneMatch trial log: first Bash call refused, final result event."""
    lines = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/x"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "x"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "ls; git status"}}]}},
        {"type": "system", "subtype": "permission_denied"},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b", "is_error": True,
                                                  "content": "Permission to use Bash has been denied"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "Bash is denied.",
         "total_cost_usd": 0.2027056, "permission_denials": [
             {"tool_name": "Bash", "tool_use_id": "b", "tool_input": {"command": "ls; git status"}}]},
    ]
    f = tmp_path / "log.jsonl"
    f.write_text("not json\n" + "\n".join(json.dumps(x) for x in lines) + "\n")
    p = wk.parse_stream(str(f))
    assert p["cost_usd"] == 0.2027056 and p["shell_calls"][0]["refused"]
    outcome, detail = wk.judge(p)
    assert outcome == "error" and "ls; git status" in detail


def test_scheduler_hooks_delegate(project, fake, tmp_path, monkeypatch):
    import supervisor as sv
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    pool = pool_for(project, fake, tmp_path)
    sch = wk.scheduler_hooks(sv.Scheduler, pool)()
    ctx = Ctx("proj")
    assert sch.start_workers(ctx, [{"task": "3"}])[0]["task"] == "3"
    end = time.time() + 15
    out = []
    while not out and time.time() < end:
        out = sch.collect(ctx)
        time.sleep(0.05)
    assert out[0]["outcome"] == "merge"
