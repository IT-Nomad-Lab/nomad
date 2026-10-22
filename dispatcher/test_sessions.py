"""Tests for POST /session (sessions.py): one resumable agent session per work package, in its own
worktree, never in the operator's checkout. Claude is faked; git is real.

Run: python3 -m pytest dispatcher/test_sessions.py -q
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("NOMAD_DISPATCH_TOKEN", "test-token")

import dispatch  # noqa: E402
import sessions  # noqa: E402


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo on master with one commit, whose main checkout sits on a feature branch (like the
    hub), and a state file of its own."""
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "master")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "README.md").write_text("hi\n")
    (r / "sub").mkdir()
    (r / "sub" / "x.txt").write_text("x\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    _git(r, "switch", "-q", "-c", "feat/operator-wip")
    monkeypatch.setattr(sessions, "SESSIONS_FILE", str(tmp_path / "state" / "sessions.json"))
    monkeypatch.setattr(sessions, "mcp_config", lambda bot: json.dumps({"as": bot}))
    return r


class FakeClaude:
    """Records each command; edits a file in its cwd when asked, like a build run would."""

    def __init__(self, edit=None, fail=None):
        self.calls, self.edit, self.fail = [], edit, list(fail or [])

    def __call__(self, cmd, cwd, **kw):
        self.calls.append({"cmd": cmd, "cwd": cwd, "env": kw.get("env", {})})
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", self.fail.pop(0))
        if self.edit:
            with open(os.path.join(cwd, self.edit), "a") as f:
                f.write("edited\n")
        out = {"result": f"done #{len(self.calls)}", "total_cost_usd": 0.12, "is_error": False}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")


def _run(repo, fake, **req):
    body = {"key": "op-42", "project": "proj", "prompt": "list the files", "bot": "nomad-dev",
            "title": "Fix the importer", **req}
    return sessions.run(body, lambda _p: {"name": "proj", "slug": "proj", "path": str(repo)},
                        dispatch.default_branch, "claude", runner=fake)


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


def test_the_first_run_makes_a_worktree_and_a_fixed_session(repo):
    fake = FakeClaude()
    r = _run(repo, fake)
    assert r["ok"], r
    wt = str(repo.resolve()) + "-wt/op-42"
    assert r["worktree"] == wt and r["branch"] == "op/42-fix-the-importer"
    assert r["resumed"] is False and r["summary"] == "done #1" and r["cost_usd"] == 0.12
    assert _git(wt, "branch", "--show-current") == "op/42-fix-the-importer"
    # Branched from the default branch, not from the operator's feature branch.
    assert _git(wt, "rev-parse", "HEAD") == _git(repo, "rev-parse", "master")
    cmd = fake.calls[0]["cmd"]
    assert fake.calls[0]["cwd"] == wt
    assert _flag(cmd, "--session-id") == sessions.session_id("op-42") and "--resume" not in cmd
    assert _flag(cmd, "--permission-mode") == "acceptEdits"
    assert "never run `git commit`" in _flag(cmd, "--append-system-prompt")
    assert json.loads(_flag(cmd, "--mcp-config")) == {"as": "nomad-dev"}
    assert fake.calls[0]["env"]["ANTHROPIC_API_KEY"] == ""
    # #1089: Sonnet by default, 1M window off, subagents on Sonnet
    assert _flag(cmd, "--model") == "sonnet"
    env = fake.calls[0]["env"]
    assert (env["CLAUDE_CODE_DISABLE_1M_CONTEXT"], env["CLAUDE_CODE_SUBAGENT_MODEL"]) == ("1", "sonnet")


def test_a_second_run_resumes_the_same_session_in_the_same_worktree(repo):
    fake = FakeClaude()
    first = _run(repo, fake)
    second = _run(repo, fake, prompt="which one did you describe first?", title="other")
    assert second["ok"] and second["resumed"] is True
    assert second["session_id"] == first["session_id"] and second["worktree"] == first["worktree"]
    assert second["branch"] == first["branch"]
    cmd = fake.calls[1]["cmd"]
    assert _flag(cmd, "--resume") == first["session_id"] and "--session-id" not in cmd
    assert sessions.load()["op-42"]["runs"] == 2


def test_edits_land_in_the_worktree_and_never_in_the_main_checkout(repo):
    r = _run(repo, FakeClaude(edit="README.md"))
    assert r["changed"] == [" M README.md"] and "README.md" in r["diffstat"]
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "branch", "--show-current") == "feat/operator-wip"


def test_plan_mode_is_read_only_with_read_only_pm_tools(repo):
    fake = FakeClaude()
    r = _run(repo, fake, mode="plan", bot="nomad-research")
    assert r["ok"] and r["mode"] == "plan"
    cmd = fake.calls[0]["cmd"]
    assert _flag(cmd, "--permission-mode") == "plan"
    assert "READ-ONLY" in _flag(cmd, "--append-system-prompt")
    tools = _flag(cmd, "--allowedTools").split(",")
    assert "mcp__nomad-pm__get" in tools
    assert not any(t.endswith(("__comment", "__transition", "__create")) for t in tools)


def test_a_project_in_a_subfolder_runs_in_that_subfolder(repo):
    fake = FakeClaude()
    r = sessions.run({"key": "op-7", "prompt": "go"},
                     lambda _p: {"name": "sub", "path": str(repo / "sub")},
                     dispatch.default_branch, "claude", runner=fake)
    assert r["ok"] and r["worktree"] == str(repo.resolve()) + "-wt/op-7"
    assert fake.calls[0]["cwd"] == r["worktree"] + "/sub"


def test_a_failed_first_run_is_not_counted_so_the_next_one_starts_fresh(repo):
    fake = FakeClaude(fail=["boom"])
    r = _run(repo, fake)
    assert not r["ok"] and r["error"] == "boom" and r["worktree"].endswith("op-42")
    r2 = _run(repo, fake)
    assert r2["ok"] and r2["resumed"] is False
    assert "--session-id" in fake.calls[1]["cmd"]


def test_a_lost_transcript_starts_a_new_session_in_the_same_worktree(repo):
    fake = FakeClaude()
    first = _run(repo, fake)
    fake.fail = ["No conversation found with session ID: x"]
    r = _run(repo, fake)
    assert r["ok"] and r["resumed"] is False
    assert r["session_id"] != first["session_id"] and r["worktree"] == first["worktree"]
    assert _run(repo, fake)["session_id"] == r["session_id"]      # and that one is kept


def test_a_first_run_on_the_usage_limit_pauses_and_the_rerun_resumes_the_session(repo):
    fake = FakeClaude(fail=["Claude AI usage limit reached|4102444800"])
    r = _run(repo, fake)
    assert not r["ok"] and r["limited"] is True and r["reset_at"] == 4102444800
    assert r["reset_clock"] == sessions.clock(4102444800)
    assert r["session_id"] == sessions.session_id("op-42")
    assert sessions.preview("op-42", str(repo), "x")["resumed"] is True
    again = _run(repo, fake)                               # the engine's rerun at the reset
    assert again["ok"] and again["resumed"] is True and again["session_id"] == r["session_id"]
    assert _flag(fake.calls[1]["cmd"], "--resume") == r["session_id"]
    assert "--session-id" not in fake.calls[1]["cmd"]
    assert fake.calls[1]["env"]["ANTHROPIC_API_KEY"] == ""
    assert sessions.load()["op-42"]["limited"] is False


def test_a_limit_hit_in_the_json_result_is_read_too(repo):
    def runner(cmd, cwd, **kw):
        out = {"result": "You've hit your limit · resets 3:30pm", "is_error": True}
        return subprocess.CompletedProcess(cmd, 1, json.dumps(out), "")
    r = _run(repo, runner)
    assert r["limited"] is True and r["reset_at"] > 0 and len(r["reset_clock"]) == 5


def test_an_ordinary_failure_is_not_a_limit(repo):
    r = _run(repo, FakeClaude(fail=["boom"]))
    assert not r["ok"] and "limited" not in r


@pytest.mark.parametrize("req,why", [
    ({"key": "op-wp-1"}, "key must be"),
    ({"key": "../x"}, "key must be"),
    ({"bot": "root"}, "bot must be"),
    ({"prompt": "  "}, "empty prompt"),
])
def test_bad_requests_are_refused_before_anything_runs(repo, req, why):
    fake = FakeClaude()
    r = _run(repo, fake, **req)
    assert not r["ok"] and why in r["error"] and not fake.calls
    assert not os.path.exists(str(repo) + "-wt")


def test_a_project_that_is_not_a_git_repo_is_refused(tmp_path, repo):
    plain = tmp_path / "site"
    plain.mkdir()
    fake = FakeClaude()
    r = sessions.run({"key": "op-1", "prompt": "go"},
                     lambda _p: {"name": "site", "path": str(plain)},
                     dispatch.default_branch, "claude", runner=fake)
    assert not r["ok"] and "not a git repo" in r["error"] and not fake.calls


def test_a_key_already_running_is_refused(repo):
    sessions._busy.add("op-42")
    try:
        r = _run(repo, FakeClaude())
    finally:
        sessions._busy.discard("op-42")
    assert not r["ok"] and r["busy"]


def test_close_removes_a_clean_worktree_and_keeps_the_branch(repo):
    r = _run(repo, FakeClaude())
    c = sessions.close({"key": "op-42"})
    assert c["ok"] and c["removed"] is True
    assert not os.path.exists(r["worktree"])
    assert _git(repo, "branch", "--list", r["branch"])
    # A later mention recreates the worktree on the same branch and resumes.
    again = _run(repo, FakeClaude())
    assert again["ok"] and again["branch"] == r["branch"] and again["resumed"] is True


def test_close_keeps_a_worktree_with_changes(repo):
    r = _run(repo, FakeClaude(edit="README.md"))
    c = sessions.close({"key": "op-42"})
    assert c["ok"] and c["removed"] is False and c["changed"] == [" M README.md"]
    assert os.path.isdir(r["worktree"])
    assert sessions.close({"key": "op-999"}) == {"ok": True, "known": False}


def test_mcp_config_acts_as_the_bot(tmp_path):
    f = tmp_path / ".mcp.json"
    f.write_text(json.dumps({"mcpServers": {"nomad-pm": {
        "command": "py", "args": ["pm.py"], "env": {"NOMAD_PM_AS": "nomad-manager", "X": "1"}}}}))
    got = json.loads(sessions.mcp_config("nomad-dev", str(f)))["mcpServers"]["nomad-pm"]
    assert got["env"] == {"NOMAD_PM_AS": "nomad-dev", "X": "1"} and got["command"] == "py"
    assert sessions.mcp_config("nomad-dev", str(tmp_path / "missing")) is None


def test_the_session_route_needs_the_token():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dispatch.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/session"
        req = urllib.request.Request(url, data=b'{"key": "op-1"}', method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 401
        req.add_header("X-Nomad-Token", dispatch.TOKEN)
        req.add_header("Content-Type", "application/json")
        body = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert body["ok"] is False                       # past auth, refused on the request
    finally:
        srv.shutdown()


BOARD = {"a-app": "multi", "a-api": "multi", "a-site": "multi", "solo": "solo-board",
         "docs": "docs-board"}


@pytest.fixture
def entries(tmp_path):
    out = []
    for pid in BOARD:
        d = tmp_path / "reg" / pid
        d.mkdir(parents=True)
        if pid not in ("a-site", "docs"):                  # the non-git ones
            _git(d, "init", "-q")
        out.append((pid, str(d)))
    return out


def _pick(entries, board, comment="", subject=""):
    return sessions.project_for(board, comment, subject, entries, lambda p: BOARD.get(p, p))


def test_a_board_with_one_git_project_needs_no_tag(entries):
    assert _pick(entries, "solo-board") == {"project": "solo"}


def test_a_board_with_several_projects_needs_a_tag(entries):
    r = _pick(entries, "multi", "@Dev fix it")
    assert "tag" in r["error"] and sorted(r["choices"]) == ["a-api", "a-app"]


def test_the_comment_tag_wins_over_the_subject_tag(entries):
    assert _pick(entries, "multi", "[a-api] fix it", "[a-app] Importer") == {"project": "a-api"}
    assert _pick(entries, "multi", "fix it", "[A-App] Importer") == {"project": "a-app"}


def test_a_tag_off_the_board_is_ignored(entries):
    assert "tag" in _pick(entries, "multi", "[solo] fix it")["error"]


def test_a_tag_naming_a_non_git_project_says_why(entries):
    r = _pick(entries, "multi", "[a-site] fix it")
    assert "not a git repo" in r["error"] and "a-app" in r["choices"]


def test_boards_with_no_git_project_or_no_project_say_why(entries):
    assert "not git repos" in _pick(entries, "docs-board")["error"]
    assert "no project" in _pick(entries, "nomad-portfolio")["error"]


def test_preview_names_the_session_and_branch_before_and_after_a_run(repo):
    p = sessions.preview("op-42", str(repo), "Fix the importer")
    assert p == {"session_id": sessions.session_id("op-42"), "branch": "op/42-fix-the-importer",
                 "resumed": False}
    _run(repo, FakeClaude())
    p = sessions.preview("op-42", str(repo), "A renamed subject")
    assert p["branch"] == "op/42-fix-the-importer" and p["resumed"] is True


def test_close_names_the_bot_that_ran_the_session(repo):
    _run(repo, FakeClaude(edit="README.md"), bot="nomad-research")
    assert sessions.close({"key": "op-42"})["bot"] == "nomad-research"
