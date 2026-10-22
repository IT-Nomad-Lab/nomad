"""Tests for permission grants (#979, docs/prd/ap-permission-grants.md section 7): the grant
parser, who may write one, which comments count, the per-run relaxation, the narrowing check,
one run per grant, approved merges through the queue and the config-path check.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot -q
"""
import json
import os
import subprocess
import sys
import time

import pytest

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, NOMAD)

from autopilot import permissions as P  # noqa: E402
from autopilot import scheduler as sc  # noqa: E402
from autopilot import tasks as tk  # noqa: E402
from autopilot import worker as wk  # noqa: E402
from autopilot.projects import Autopilot, Command, ConfigError, Project  # noqa: E402

OP, AGENT, MANAGER = 4, 9, 7


def act(raw, user=OP, aid=1, created="2026-10-02T10:00:00Z", moved=None):
    a = {"id": aid, "createdAt": created, "comment": {"raw": raw},
         "_links": {"user": {"href": f"/api/v3/users/{user}"}}}
    if moved:
        a["details"] = [{"raw": f"Status changed from In Progress to {moved}"}]
    return a


def lines(text):
    return [(g.action, g.arg) if g else None for _, g, _ in tk.grant_lines(text)]


# ---- parser ----------------------------------------------------------------------------------

def test_each_of_the_four_forms_parses():
    text = ("grant: edit CLAUDE.md\ngrant: delete branch feat/old\n"
            "grant: delete worktree /wt/ap-2\ngrant: merge feat/9-x")
    assert lines(text) == [("edit", "CLAUDE.md"), ("delete branch", "feat/old"),
                           ("delete worktree", "/wt/ap-2"), ("merge", "feat/9-x")]


def test_the_keyword_is_case_insensitive_and_the_argument_is_kept_as_written():
    assert lines("GRANT: Delete  Branch Feat/Old") == [("delete branch", "Feat/Old")]
    assert lines("  Grant:   EDIT  docs/X.md  ") == [("edit", "docs/X.md")]


@pytest.mark.parametrize("line", [
    "grant: edit all the docs", "grant: edit", "grant: delete feat/old",
    "grant: merge", "grant: push master", "grant:edit CLAUDE.md",
])
def test_a_malformed_grant_line_is_refused_with_a_reason(line):
    [(raw, g, why)] = tk.grant_lines(line)
    assert g is None and raw == line.strip() and "not one of" in why


def test_quoted_and_fenced_lines_never_count():
    text = ("> grant: edit CLAUDE.md\n```\ngrant: merge feat/x\n```\n~~~\ngrant: edit A.md\n~~~\n"
            "grant: edit B.md")
    assert lines(text) == [("edit", "B.md")]


def test_several_grants_in_one_comment():
    assert len(lines("ok, go.\ngrant: edit A.md\ngrant: edit .claude/settings.json\n")) == 2


@pytest.mark.parametrize("text", [
    "you can delete the old branch", "approve, and merge it", "please grant edit on CLAUDE.md",
    "the grant: edit CLAUDE.md line goes in a comment",
])
def test_free_text_never_matches(text):
    assert tk.grant_lines(text) == []


# ---- authorship and scope --------------------------------------------------------------------

def test_an_operator_comment_counts_and_an_agent_comment_does_not():
    grants, refused = tk.task_grants([act("grant: edit CLAUDE.md", OP, 1),
                                      act("grant: edit AGENTS.md", AGENT, 2)], False,
                                     operator_id=OP, manager_id=MANAGER)
    assert [(g.arg, g.activity_id) for g in grants] == [("CLAUDE.md", 1)]
    assert refused == []


def test_a_relayed_prefix_counts_only_from_the_engine_account():
    acts = [act("**Operator, via Zulip:** grant: edit A.md", MANAGER, 1),
            act("**Operator, via Zulip:** grant: edit B.md", AGENT, 2)]
    grants, _ = tk.task_grants(acts, False, operator_id=OP, manager_id=MANAGER)
    assert [g.arg for g in grants] == ["A.md"]
    grants, _ = tk.task_grants(acts, False, operator_id=OP, manager_id=None)
    assert grants == []


def test_delete_and_merge_count_only_inside_the_approved_answer():
    acts = [act("grant: delete branch feat/early", OP, 1),
            act("question?", AGENT, 2, moved="Awaiting approval"),
            act("approve\ngrant: delete branch feat/old\ngrant: merge feat/9-x\n"
                "grant: edit CLAUDE.md", OP, 3)]
    grants, refused = tk.task_grants(acts, True, operator_id=OP, manager_id=MANAGER)
    assert [(g.action, g.arg) for g in grants] == [
        ("delete branch", "feat/old"), ("merge", "feat/9-x"), ("edit", "CLAUDE.md")]
    assert [r.line for r in refused] == ["grant: delete branch feat/early"]
    assert "Approved answer" in refused[0].reason
    grants, refused = tk.task_grants(acts, False, operator_id=OP, manager_id=MANAGER)
    assert [(g.action, g.arg) for g in grants] == [("edit", "CLAUDE.md")]   # not Approved
    assert len(refused) == 3


class FakePM:
    """The tasks.fetch surface for one task, with the given description and activities."""

    def __init__(self, status, description, activities):
        self.wp = {"id": 50, "subject": "x", "description": {"raw": description},
                   "_links": {"status": {"title": status}, "type": {"title": "Task"},
                              "priority": {"title": "Normal"}}}
        self.acts = activities
        self.posted = []
        self.moves = []

    def open_work_packages(self, board):
        return [self.wp]

    def lane_key(self, board):
        return None

    def block_relations(self):
        return []

    def work_package(self, wp_id):
        return self.wp

    def closed(self, status):
        return status in tk.CLOSED

    def manager_id(self):
        return MANAGER

    def activities(self, wp_id):
        return self.acts

    def comments(self, wp_id):
        return [(a.get("comment") or {}).get("raw") or "" for a in self.acts]

    def comment(self, wp_id, text):
        self.posted.append((wp_id, text))

    def transition(self, wp_id, status):
        self.moves.append((wp_id, status))


def test_description_text_never_counts(monkeypatch):
    monkeypatch.setattr(tk, "OPERATOR_ID", OP)
    pm = FakePM("Ready", "Done means: x\n\ngrant: edit CLAUDE.md\n", [act("go", OP, 1)])
    [t] = tk.fetch(pm, "b")
    assert t.grants == [] and "grants" not in t.as_dict()


def test_fetch_carries_operator_grants_on_the_task():
    pm = FakePM("Ready", "Done means: x", [act("grant: edit CLAUDE.md", OP, 11)])
    [t] = tk.fetch(pm, "b")
    assert t.as_dict()["grants"] == [{"action": "edit", "arg": "CLAUDE.md", "activity_id": 11,
                                      "created": "2026-10-02T10:00:00Z"}]


def test_an_edit_grant_from_before_the_last_run_started_is_ignored(tmp_path):
    ledger = wk.GrantLedger(str(tmp_path / "grants.json"))
    task = {"task": "50", "grants": [
        {"action": "edit", "arg": "A.md", "activity_id": 1, "created": "2026-10-02T09:00:00Z"},
        {"action": "edit", "arg": "B.md", "activity_id": 2, "created": "2026-10-02T11:00:00Z"}]}
    started = wk._iso_epoch("2026-10-02T10:00:00Z")
    assert [g.arg for g in wk.usable_grants(task, ledger, "p", started)] == ["B.md"]
    assert [g.arg for g in wk.usable_grants(task, ledger, "p", 0)] == ["A.md", "B.md"]


# ---- rendering --------------------------------------------------------------------------------

def _project(tmp_path):
    repo = tmp_path / "p"
    repo.mkdir(exist_ok=True)
    ap = Autopilot(test={"default": Command("scripts/t.sh {filter}")},
                   pre_merge_gate=(Command("scripts/t.sh {filter}"),), milestone_gate=(),
                   worktree_root=str(tmp_path / "p-wt"))
    return Project(id="p", path=str(repo), autopilot=ap)


def _facts(**kw):
    base = dict(default_branch="master", own_branch="feat/1-x",
                files=("CLAUDE.md", "sub/CLAUDE.md", "AGENTS.md", "src/a.py"),
                checked_out=frozenset({"master", "feat/1-x", "feat/busy"}), worktrees=frozenset())
    base.update(kw)
    return P.GrantFacts(**base)


def _render(tmp_path, grants, facts=None):
    p = _project(tmp_path)
    wt = str(tmp_path / "p-wt" / "ap-1")
    st = str(tmp_path / "st")
    s, applied, refused = P.build_settings_with_grants(
        p, wt, state_dir=st, default_branch="master",
        grants=[tk.Grant(a, x, i + 1) for i, (a, x) in enumerate(grants)],
        facts=facts or _facts())
    plain = P.build_settings(p, wt, state_dir=st, default_branch="master")
    return s, plain, wt, p, applied, refused


def _diff(s, plain):
    a, b = s["permissions"], plain["permissions"]
    return ([r for r in a["allow"] if r not in b["allow"]],
            [r for r in b["deny"] if r not in a["deny"]],
            [r for r in a["deny"] if r not in b["deny"]])


def test_edit_grant_opens_that_file_and_no_other_claude_md(tmp_path):
    s, plain, wt, p, applied, refused = _render(tmp_path, [("edit", "CLAUDE.md")])
    assert len(applied) == 1 and refused == []
    added, dropped, redenied = _diff(s, plain)
    assert added == [f"Edit(/{wt}/CLAUDE.md)"]
    assert dropped == ["Edit(**/CLAUDE.md)", "Edit(//**/CLAUDE.md)"]
    assert redenied == [f"Edit(/{wt}/sub/CLAUDE.md)"]
    assert P.decide_edit(s, f"{wt}/CLAUDE.md", cwd=wt) == "allow"
    assert P.decide_edit(s, f"{wt}/sub/CLAUDE.md", cwd=wt) == "deny"
    assert P.decide_edit(s, f"{wt}/AGENTS.md", cwd=wt) == "deny"
    assert P.decide_edit(s, f"{p.repo}/CLAUDE.md", cwd=wt) == "deny"
    assert P.decide_edit(plain, f"{wt}/CLAUDE.md", cwd=wt) == "deny"


def test_edit_grant_for_a_file_in_claude_folder(tmp_path):
    s, plain, wt, *_ = _render(tmp_path, [("edit", ".claude/settings.json")],
                               _facts(files=(".claude/settings.json", ".claude/agents/x.md")))
    _, dropped, redenied = _diff(s, plain)
    assert dropped == ["Edit(**/.claude/**)", "Edit(//**/.claude/**)"]
    assert redenied == [f"Edit(/{wt}/.claude/agents/x.md)"]
    assert P.decide_edit(s, f"{wt}/.claude/settings.json", cwd=wt) == "allow"
    assert P.decide_edit(s, f"{wt}/.claude/agents/x.md", cwd=wt) == "deny"
    assert P.decide_edit(s, os.path.expanduser("~/.claude/settings.json"), cwd=wt) == "deny"


def test_delete_branch_grant_opens_exactly_that_delete(tmp_path):
    s, plain, wt, *_ = _render(tmp_path, [("delete branch", "feat/old")])
    added, dropped, _ = _diff(s, plain)
    assert added == [f"Bash(git -C {wt} branch -d feat/old)"]
    assert dropped == [f"Bash(git -C {wt} branch -d *)"]
    assert P.decide_bash(s, f"git -C {wt} branch -d feat/old") == "allow"
    assert P.decide_bash(s, f"git -C {wt} branch -d other") == "refuse"
    assert P.decide_bash(s, f"git -C {wt} branch -d feat/old other") == "refuse"
    for c in (f"git -C {wt} branch -D feat/old", f"git -C {wt} branch --delete feat/old",
              "git branch -d feat/old"):
        assert P.decide_bash(s, c) == "deny", c


def test_delete_worktree_grant_opens_exactly_that_remove(tmp_path):
    other = str(tmp_path / "p-wt" / "ap-2")
    s, plain, wt, *_ = _render(tmp_path, [("delete worktree", other)],
                               _facts(worktrees=frozenset({other})))
    added, dropped, _ = _diff(s, plain)
    assert added == [f"Bash(git -C {wt} worktree remove {other})"]
    assert dropped == ["Bash(git worktree remove *)"]
    assert P.decide_bash(s, f"git -C {wt} worktree remove {other}") == "allow"
    assert P.decide_bash(s, f"git -C {wt} worktree remove --force {other}") == "refuse"
    assert P.decide_bash(s, f"git -C {wt} worktree remove {tmp_path}/p-wt/ap-3") == "refuse"
    assert P.decide_bash(plain, f"git worktree remove {other}") == "deny"


@pytest.mark.parametrize("grant,match", [
    (("edit", "/abs/CLAUDE.md"), "relative"), (("edit", "~/x"), "relative"),
    (("edit", "docs/*.md"), "glob"), (("edit", "../x/CLAUDE.md"), r"\.\."),
    (("edit", "sub"), "folder"), (("edit", "a b"), "character"),
    (("delete branch", "master"), "default branch"), (("delete branch", "main"), "default"),
    (("delete branch", "feat/1-x"), "own branch"), (("delete branch", "feat/busy"), "checked out"),
    (("delete branch", "-D"), "not a valid"), (("delete branch", "a..b"), "not a valid"),
    (("delete worktree", "/elsewhere/ap-2"), "worktree_root"),
    (("delete worktree", "{wt}"), "own worktree"),
    (("delete worktree", "{root}/ap-9"), "worktree list"),
    (("merge", "feat/x"), "merge queue"),
])
def test_a_grant_that_breaks_an_argument_rule_opens_nothing(tmp_path, grant, match):
    (tmp_path / "p-wt" / "ap-1" / "sub").mkdir(parents=True)
    root, wt = str(tmp_path / "p-wt"), str(tmp_path / "p-wt" / "ap-1")
    action, arg = grant[0], grant[1].format(wt=wt, root=root)
    s, plain, _, _, applied, refused = _render(tmp_path, [(action, arg)])
    assert applied == [] and len(refused) == 1
    assert __import__("re").search(match, refused[0][1]), refused[0][1]
    assert s == plain


def test_a_merge_allow_on_the_main_checkout_never_appears(tmp_path):
    s, _, _, p, *_ = _render(tmp_path, [("edit", "CLAUDE.md"), ("delete branch", "feat/old"),
                                        ("merge", "feat/old")])
    assert f"Bash(git -C {p.repo} merge *)" in s["permissions"]["deny"]
    assert not [r for r in s["permissions"]["allow"]
                if r.startswith(f"Bash(git -C {p.repo} merge")]
    assert P.decide_bash(s, f"git -C {p.repo} merge --ff-only feat/old") == "deny"


def test_no_grants_render_exactly_the_plain_profile(tmp_path):
    p = _project(tmp_path)
    wt, st = str(tmp_path / "p-wt" / "ap-1"), str(tmp_path / "st")
    plain = P.build_settings(p, wt, state_dir=st, default_branch="master")
    assert P.build_settings(p, wt, state_dir=st, default_branch="master", grants=()) == plain
    assert json.dumps(P.build_settings_with_grants(p, wt, st, None, "master")[0]) == \
        json.dumps(plain)


# ---- narrowing --------------------------------------------------------------------------------

def test_narrowing_accepts_only_what_a_grant_accounts_for(tmp_path):
    s, _, wt, p, *_ = _render(tmp_path, [("delete branch", "feat/old")])
    st = str(tmp_path / "st")
    t = P.render_template(wt, p.repo, st, branches=P.protected_branches(p.repo, "master"))
    ws = P.wrappers(p, st)
    with pytest.raises(ConfigError, match="beyond the template"):
        P.check_narrowing(s, t, ws)                  # the grant's allow, without the grant
    allow = [f"Bash(git -C {wt} branch -d feat/old)"]
    drop = [f"Bash(git -C {wt} branch -d *)"]
    P.check_narrowing(s, t, ws, allow, drop)
    s["permissions"]["allow"].append(f"Bash(git -C {wt} branch -d other)")
    with pytest.raises(ConfigError, match="beyond the template"):
        P.check_narrowing(s, t, ws, allow, drop)
    s["permissions"]["allow"].pop()
    s["permissions"]["deny"].remove("Bash(git push *)")
    with pytest.raises(ConfigError, match="dropped"):
        P.check_narrowing(s, t, ws, allow, drop)


# ---- one run, launch, config check (real git, fake claude) -----------------------------------

def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


FAKE = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["FAKE_ARGV"], "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:]}}) + "\\n")
if os.environ.get("FAKE_TOUCH"):
    open(os.environ["FAKE_TOUCH"], "a").write("x\\n")
    os.system("git add -A && git -c user.email=t@t -c user.name=t commit -qm touch")
text = "done\\n\\nautopilot: result ready to merge"
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": text}}]}}}}))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": text,
                  "total_cost_usd": 0.1, "permission_denials": []}}))
"""


@pytest.fixture
def live(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.email", "t@example.com")
    sh(repo, "git", "config", "user.name", "t")
    (repo / "CLAUDE.md").write_text("rules\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "CLAUDE.md").write_text("more rules\n")
    sh(repo, "git", "add", ".")
    sh(repo, "git", "commit", "-qm", "init")
    sh(repo, "git", "switch", "-qc", "feat/other")
    sh(repo, "git", "branch", "feat/old", "main")
    ap = Autopilot(test={"default": Command("pytest -q {filter}")},
                   pre_merge_gate=(Command("pytest -q {filter}"),), milestone_gate=(),
                   worktree_root=str(tmp_path / "proj-wt"))
    project = Project(id="proj", path=str(repo), autopilot=ap)
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_ARGV", str(tmp_path / "argv.jsonl"))
    pool = wk.WorkerPool(project_of=lambda pid: project if pid == "proj" else None,
                         claude=str(fake), state_dir=str(tmp_path / "state"))
    return project, pool, tmp_path


class Ctx:
    def __init__(self, pid="proj", project=None):
        self.project_id = pid
        self.project = project or {"workers": []}
        self.events = []
        self.slots = 1

    def event(self, kind, msg):
        self.events.append((kind, msg))


def _wait(pool, ctx):
    end = time.time() + 15
    while time.time() < end:
        out = pool.collect(ctx)
        if out:
            return out
        time.sleep(0.05)
    raise AssertionError("worker never finished")


def _last_call(tmp_path):
    with open(tmp_path / "argv.jsonl") as f:
        argv = json.loads(f.read().splitlines()[-1])["argv"]
    return argv[1], json.load(open(argv[argv.index("--settings") + 1]))


def _grant(arg, aid, action="edit", created="2099-01-01T00:00:00Z"):
    return {"action": action, "arg": arg, "activity_id": aid, "created": created}


def test_a_grant_opens_one_run_and_the_next_run_is_refused_again(live):
    project, pool, tmp_path = live
    ctx = Ctx()
    task = {"task": "5", "title": "rules", "grants": [_grant("CLAUDE.md", 31),
                                                      _grant("feat/old", 32, "delete branch")]}
    pool.start_workers(ctx, [task])
    _wait(pool, ctx)
    wt = os.path.join(project.autopilot.worktree_root, "ap-5")
    prompt, s = _last_call(tmp_path)
    assert "For this run only, the operator granted:" in prompt
    assert "`grant: edit CLAUDE.md`" in prompt and "`grant: delete branch feat/old`" in prompt
    assert P.decide_edit(s, f"{wt}/CLAUDE.md", cwd=wt) == "allow"
    assert P.decide_edit(s, f"{wt}/sub/CLAUDE.md", cwd=wt) == "deny"
    assert P.decide_bash(s, f"git -C {wt} branch -d feat/old") == "allow"
    pool.start_workers(ctx, [task])                  # same grants: already used
    _wait(pool, ctx)
    prompt, s = _last_call(tmp_path)
    assert "operator granted" not in prompt
    assert P.decide_edit(s, f"{wt}/CLAUDE.md", cwd=wt) == "deny"
    assert P.decide_bash(s, f"git -C {wt} branch -d feat/old") == "deny"


def test_a_failed_launch_uses_no_grant(live, monkeypatch):
    project, pool, tmp_path = live
    pool.claude = str(tmp_path / "no-such-claude")
    task = {"task": "6", "title": "x", "grants": [_grant("CLAUDE.md", 41)]}
    with pytest.raises(OSError):
        pool.launch(project, task)
    assert pool.ledger.used_keys("proj", "6") == set()


def test_refused_grants_are_reported_by_the_run(live):
    project, pool, tmp_path = live
    ctx = Ctx()
    task = {"task": "7", "title": "x", "grants": [_grant("main", 51, "delete branch")]}
    pool.start_workers(ctx, [task])
    run = pool.runs[("proj", "7")]
    assert run.reported["grants"] == []
    assert run.reported["grants_refused"][0]["line"] == "grant: delete branch main"
    _wait(pool, ctx)


def test_an_ungranted_claude_md_change_gives_needs_operator(live, monkeypatch):
    project, pool, tmp_path = live
    wt = os.path.join(project.autopilot.worktree_root, "ap-8")
    monkeypatch.setenv("FAKE_TOUCH", os.path.join(wt, "sub", "CLAUDE.md"))
    ctx = Ctx()
    pool.start_workers(ctx, [{"task": "8", "title": "x"}])
    [out] = _wait(pool, ctx)
    assert out["outcome"] == "needs_operator"
    assert out["config_changes"] == ["sub/CLAUDE.md"]
    assert "which no grant named" in out["detail"]


def test_a_granted_claude_md_change_passes(live, monkeypatch):
    project, pool, tmp_path = live
    wt = os.path.join(project.autopilot.worktree_root, "ap-9")
    monkeypatch.setenv("FAKE_TOUCH", os.path.join(wt, "CLAUDE.md"))
    ctx = Ctx()
    pool.start_workers(ctx, [{"task": "9", "title": "x", "grants": [_grant("CLAUDE.md", 61)]}])
    [out] = _wait(pool, ctx)
    assert out["outcome"] == "merge" and "config_changes" not in out


def test_an_ordinary_change_passes_the_config_check(live, monkeypatch):
    project, pool, tmp_path = live
    wt = os.path.join(project.autopilot.worktree_root, "ap-10")
    monkeypatch.setenv("FAKE_TOUCH", os.path.join(wt, "notes.txt"))
    ctx = Ctx()
    pool.start_workers(ctx, [{"task": "10", "title": "x"}])
    [out] = _wait(pool, ctx)
    assert out["outcome"] == "merge"


def test_the_merge_queue_refuses_an_ungranted_config_change(live):
    from autopilot.merge import MergeQueue

    project, pool, tmp_path = live
    wt = wk.ensure_worktree(project.repo, project.autopilot.worktree_root, "11", "x")
    with open(os.path.join(wt.path, "CLAUDE.md"), "a") as f:
        f.write("changed\n")
    sh(wt.path, "git", "commit", "-qam", "edit rules")

    class Tracker:
        moves = []

        def move(self, task, status, comment):
            self.moves.append((task, status, comment))

    calls = []
    q = MergeQueue(project_of=lambda pid: project, tracker=Tracker(), background=False,
                   state_dir=str(tmp_path / "state"),
                   runner=lambda *a, **k: calls.append(a) or (0, ""))
    [res] = q.merge(Ctx(), [{"task": "11", "outcome": "merge", "branch": wt.branch,
                             "worktree": wt.path}])
    assert res["result"] == "needs_operator" and calls == []      # the gate never ran
    assert "CLAUDE.md" in Tracker.moves[-1][2] and Tracker.moves[-1][1] == "Awaiting approval"
    # granted in an earlier run: the same branch is no longer refused for that path
    wk.GrantLedger(str(tmp_path / "state" / "grants.json")).use(
        "proj", "11", [tk.Grant("edit", "CLAUDE.md", 70)])
    assert wk.config_changes(project, wt.path, {"CLAUDE.md"}, str(tmp_path / "state")) == []


# ---- approved merges (scheduler) --------------------------------------------------------------

class Merger:
    def __init__(self):
        self.queued = []

    def merge(self, ctx, finished):
        self.queued += [f for f in finished if f.get("outcome") == "merge"]

    def busy(self, pid):
        return [f["task"] for f in self.queued]


def _approved_task(grant_text, tid=50):
    t = tk.Task(id=tid, subject="x", status="Approved", type="Task")
    acts = [act("q", AGENT, 1, moved="Awaiting approval"), act(grant_text, OP, 2)]
    t.grants, t.refused = tk.task_grants(acts, True, operator_id=OP, manager_id=MANAGER)
    return t


def _scheduler(live, merger):
    project, pool, tmp_path = live
    pm = FakePM("Approved", "", [])
    s = sc.AutopilotScheduler(pm=pm, registry=lambda pid: project, pool=pool, merger=merger,
                              listing=lambda: [project])
    return s, pm


def test_a_merge_grant_queues_the_branch_and_starts_no_worker(live):
    project, pool, tmp_path = live
    wt = wk.ensure_worktree(project.repo, project.autopilot.worktree_root, "50", "x")
    merger = Merger()
    s, pm = _scheduler(live, merger)
    t = _approved_task(f"approve\ngrant: merge {wt.branch}")
    sel = tk.Selection([t], [])
    assert s._merge_granted(Ctx(), project, sel) is True
    assert sel.qualified == [] and sel.skipped[0].kind == "running"
    assert merger.queued == [{"task": "50", "outcome": "merge", "branch": wt.branch,
                              "worktree": wt.path}]
    assert pm.moves == [(50, "In Progress")]
    assert pm.posted[0][1].startswith(f"autopilot: merge granted {wt.branch}")
    assert pool.runs == {}
    sel = tk.Selection([t], [])                       # used: the next pass starts nothing new
    assert s._merge_granted(Ctx(), project, sel) is False and len(merger.queued) == 1


@pytest.mark.parametrize("branch,why", [("main", "default branch"),
                                        ("feat/old", "no worktree under")])
def test_a_merge_grant_without_a_valid_target_is_ignored_once(live, branch, why):
    project, pool, tmp_path = live
    merger = Merger()
    s, pm = _scheduler(live, merger)
    t = _approved_task(f"grant: merge {branch}")
    for _ in range(2):
        assert s._merge_granted(Ctx(), project, tk.Selection([t], [])) is False
    assert merger.queued == [] and pm.moves == []
    assert len(pm.posted) == 1
    assert pm.posted[0][1].startswith(f"autopilot: grant ignored: grant: merge {branch} (")
    assert why in pm.posted[0][1]


def test_refused_lines_are_reported_once_per_line(live):
    s, pm = _scheduler(live, Merger())
    t = tk.Task(id=60, subject="x", status="Ready", type="Task")
    t.grants, t.refused = tk.task_grants([act("grant: edit all the docs\ngrant: merge x", OP, 3)],
                                         False, operator_id=OP, manager_id=MANAGER)
    s._report_refused(Ctx(), [t])
    s._report_refused(Ctx(), [t])
    assert [p[1].split(" (")[0] for p in pm.posted] == [
        "autopilot: grant ignored: grant: edit all the docs",
        "autopilot: grant ignored: grant: merge x"]


def test_a_config_change_moves_the_task_to_awaiting_approval(live):
    s, pm = _scheduler(live, Merger())
    s._note_config(Ctx(), "70", {"branch": "feat/70-x", "config_changes": ["CLAUDE.md"]})
    assert pm.moves == [(70, "Awaiting approval")]
    assert "`CLAUDE.md`" in pm.posted[0][1] and "Not queued" in pm.posted[0][1]
