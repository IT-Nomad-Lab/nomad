"""Tests for the merge queue (#871): real git in temp repos (a main checkout plus a task
worktree), fake gate commands run through the generated wrappers, a fake tracker.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest autopilot -q
"""
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, NOMAD)

from autopilot import merge as mq  # noqa: E402
from autopilot.projects import Autopilot, Command, Project  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "nunit_unity_sample.xml")

FAKE_TEST = r'''#!/usr/bin/env python3
"""Fake unity.sh test: behaviour per test from $FAKE_CFG; writes NUnit XML under Logs/."""
import json, os, sys
cfg = json.load(open(os.environ["FAKE_CFG"]))
filt = sys.argv[1] if len(sys.argv) > 1 else ""
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps({"cmd": "test", "filter": filt, "cwd": os.getcwd(),
                        "argv0": os.environ.get("FAKE_ARGV0", "")}) + "\n")
n = sum(1 for _ in open(os.environ["FAKE_CALLS"]))
tests = cfg["tests"]
if filt:
    tests = {k: v for k, v in tests.items() if k == filt}
res = {"pass": "Passed", "fail": "Failed", "skip": "Skipped",
       "flaky": "Passed" if filt else "Failed"}
cases = "".join('<test-case fullname="%s" name="%s" result="%s" />' % (k, k.rsplit(".", 1)[-1], res[v])
                for k, v in tests.items())
failed = sum(1 for v in tests.values() if res[v] == "Failed")
os.makedirs("Logs", exist_ok=True)
path = os.path.abspath("Logs/test_%d.xml" % n)
with open(path, "w") as f:
    f.write('<?xml version="1.0"?><test-run result="%s" total="%d">'
            '<test-suite type="Assembly">%s</test-suite></test-run>'
            % ("Failed(Child)" if failed else "Passed", len(tests), cases))
if cfg.get("hook") and not filt:
    os.system(cfg.pop("hook"))
    json.dump(cfg, open(os.environ["FAKE_CFG"], "w"))
print("unity exit=0 (results: %s)" % path)
sys.exit(cfg.get("test_exit", 0))
'''

FAKE_STEP = r'''#!/usr/bin/env python3
import json, os, sys
cfg = json.load(open(os.environ["FAKE_CFG"]))
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps({"cmd": sys.argv[1], "cwd": os.getcwd()}) + "\n")
if sys.argv[1] == "step":
    print("compiling"); sys.exit(cfg.get("step_exit", 0))
if sys.argv[1] == "suite":
    print("ran the suite"); sys.exit(cfg.get("suite_exit", 0))
'''


def sh(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class FakeTracker:
    def __init__(self):
        self.moves = []

    def move(self, task, status, comment):
        self.moves.append((str(task), status, comment))

    def comment(self, task, text):
        self.moves.append((str(task), None, text))


class Ctx:
    def __init__(self, pid="p"):
        self.project_id = pid
        self.events = []

    def event(self, kind, msg):
        self.events.append((kind, msg))


class Env:
    """A main checkout on master, a worktree root, fake gate scripts and a config file."""

    def __init__(self, tmp, monkeypatch):
        self.tmp = tmp
        for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                     "GIT_COMMITTER_EMAIL": "t@x"}.items():
            monkeypatch.setenv(k, v)
        self.repo = tmp / "repo"
        self.repo.mkdir()
        sh(self.repo, "init", "-q", "-b", "master")
        (self.repo / "a.txt").write_text("one\ntwo\nthree\n")
        (self.repo / "notes.txt").write_text("operator notes\n")
        (self.repo / ".gitignore").write_text("Logs/\n")
        sh(self.repo, "add", ".")
        sh(self.repo, "commit", "-qm", "init")
        self.wt_root = tmp / "repo-wt"
        self.wt_root.mkdir()
        self.state_dir = tmp / "state"
        self.bin = tmp / "fakes"
        self.bin.mkdir()
        (self.bin / "fake_test.py").write_text(FAKE_TEST)
        (self.bin / "fake_step.py").write_text(FAKE_STEP)
        self.cfg_path = tmp / "cfg.json"
        self.calls = tmp / "calls.jsonl"
        self.calls.write_text("")
        monkeypatch.setenv("FAKE_CFG", str(self.cfg_path))
        monkeypatch.setenv("FAKE_CALLS", str(self.calls))
        self.config(tests={"T.A1_ok": "pass", "T.A2_ok": "pass"})
        self.tracker = FakeTracker()
        self.n = 0

    def config(self, **cfg):
        self.cfg_path.write_text(json.dumps(cfg))

    def project(self, results="nunit", gate=None, flakes=(), known=(), post=True):
        unit = Command(f"python3 {self.bin}/fake_test.py {{filter}}")
        step = Command(f"python3 {self.bin}/fake_step.py step")
        suite = Command(f"python3 {self.bin}/fake_step.py suite")
        tests = {"unit": unit} if results == "nunit" else {"unit": suite}
        pre = gate if gate is not None else (step, tests["unit"])
        ap = Autopilot(test=tests, pre_merge_gate=pre, milestone_gate=pre,
                       post_test=(Command(f"python3 {self.bin}/fake_step.py post"),) if post else (),
                       flake_set=frozenset(flakes), known_failures=frozenset(known),
                       worktree_root=str(self.wt_root), results=results)
        return Project(id="p", path=str(self.repo), autopilot=ap)

    def task(self, tid="1", content="one\ntwo\nthree\nfour\n", fname="a.txt"):
        """A worktree on feat/<tid>-x with one commit, as a worker leaves it."""
        wt = self.wt_root / f"ap-{tid}"
        sh(self.repo, "worktree", "add", "-q", "-b", f"feat/{tid}-x", str(wt), "master")
        (wt / fname).write_text(content)
        sh(wt, "add", fname)
        sh(wt, "commit", "-qm", f"task {tid}")
        return {"task": tid, "outcome": "merge", "branch": f"feat/{tid}-x", "worktree": str(wt)}

    def queue(self, project, **kw):
        return mq.MergeQueue(project_of=lambda pid: project, tracker=self.tracker,
                             state_dir=str(self.state_dir), background=False, **kw)

    def calls_list(self):
        return [json.loads(x) for x in self.calls.read_text().splitlines() if x.strip()]

    def head(self):
        return sh(self.repo, "rev-parse", "master")


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ---- results parsing ---------------------------------------------------------------------------

def test_parse_nunit_counts_failures_and_skips():
    r = mq.parse_nunit(FIXTURE)
    assert (r.total, r.passed, r.failed, r.skipped) == (6, 2, 2, 2)   # inconclusive counts as skipped
    assert r.failures == ["RuneMerge.Tests.PowerUpWildBurstTests.PWR2_5_LodestoneGathersItsOwnColor",
                          "RuneMerge.Tests.LevelCurveTests.CV5a_BossFractionsSitInTheSameBand"]
    assert "RuneMerge.Tests.AbilityTests.AB1_Hidden_WithoutProfile" in r.passed_names
    assert r.result == "Failed(Child)"


def test_parse_nunit_rejects_garbage(tmp_path):
    bad = tmp_path / "x.xml"
    bad.write_text("<not-nunit/>")
    with pytest.raises(mq.ResultsError):
        mq.parse_nunit(str(bad))
    bad.write_text("<test-run")
    with pytest.raises(mq.ResultsError):
        mq.parse_nunit(str(bad))


def test_find_results_uses_the_output_then_the_glob_and_never_a_stale_file(tmp_path):
    logs = tmp_path / "Logs"
    logs.mkdir()
    old = logs / "test_old.xml"
    old.write_text("x")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    since = time.time()
    new = logs / "test_new.xml"
    new.write_text("x")
    out = f"unity exit=0 (results: {new})\n"
    assert mq.find_results(out, str(tmp_path), since) == str(new)
    assert mq.find_results("no path here", str(tmp_path), since, "Logs/test_*.xml") == str(new)
    assert mq.find_results(f"results: {old}", str(tmp_path), since) is None
    new.unlink()
    assert mq.find_results("", str(tmp_path), since, "Logs/test_*.xml") is None


# ---- the happy path ------------------------------------------------------------------------------

def test_green_gate_fast_forwards_and_marks_done(env):
    env.config(tests={"T.A1_ok": "pass", "T.A2_ok": "pass", "T.S1": "skip"})
    p = env.project()
    f = env.task()
    res = env.queue(p).merge(Ctx(), [f])
    assert [r["result"] for r in res] == ["merged"]
    branch_head = sh(f["worktree"], "rev-parse", "HEAD")
    assert env.head() == branch_head == res[0]["commit"]
    assert (env.repo / "a.txt").read_text().endswith("four\n")      # main checkout moved too
    tid, status, comment = env.tracker.moves[-1]
    assert (tid, status) == ("1", "Done")
    assert branch_head in comment and "Not pushed" in comment
    assert "| unit | 3 | 2 | 0 | 1 | pass |" in comment
    assert "fake_step.py step" in comment and "Flakes rerun (passed 3 solo reruns): none" in comment
    # every command went through the generated wrappers, in the worktree
    calls = env.calls_list()
    assert [c["cmd"] for c in calls] == ["step", "test", "post"]
    assert all(c["cwd"] == f["worktree"] for c in calls)
    assert os.path.isfile(env.state_dir / "bin" / "p-test-unit")
    # the first green gate records the baseline
    st = json.loads((env.state_dir / "merge-state.json").read_text())
    assert st["projects"]["p"]["baseline"] == {"unit": 3}
    assert st["projects"]["p"]["queue"] == []


def test_default_is_merged_into_the_branch_first(env):
    p = env.project()
    f = env.task(content="one\ntwo\nthree\nfour\n")
    (env.repo / "b.txt").write_text("from master\n")
    sh(env.repo, "add", "b.txt")
    sh(env.repo, "commit", "-qm", "master moved")
    res = env.queue(p).merge(Ctx(), [f])
    assert res[0]["result"] == "merged"
    assert (env.repo / "b.txt").exists() and (env.repo / "a.txt").read_text().endswith("four\n")
    assert sh(env.repo, "rev-list", "--count", "master") == "4"   # init, master moved, task, merge


def test_only_merge_outcomes_are_queued(env):
    p = env.project()
    q = env.queue(p)
    assert q.merge(Ctx(), [{"task": "9", "outcome": "resume"},
                           {"task": "8", "outcome": "failed"}]) == []
    assert env.tracker.moves == [] and env.calls_list() == []


def test_exit_code_only_suite(env):
    p = env.project(results="exit")
    res = env.queue(p).merge(Ctx(), [env.task()])
    assert res[0]["result"] == "merged"
    assert "| unit | - | - | - | - | pass (exit 0) |" in env.tracker.moves[-1][2]
    env.config(suite_exit=1)
    res = env.queue(p).merge(Ctx(), [env.task("2", content="x\n", fname="c.txt")])
    assert res[0]["result"] == "resume" and "unit: exit 1" in res[0]["failing"][0]


# ---- failures, flakes, known failures, counts ---------------------------------------------------

def test_known_failures_and_flakes_that_pass_solo_do_not_block(env):
    env.config(tests={"T.A1_ok": "pass", "T.CV5a_red": "fail", "T.BLK6_flaky": "flaky"})
    p = env.project(flakes=["BLK6"], known=["CV5a"])
    res = env.queue(p).merge(Ctx(), [env.task()])
    assert res[0]["result"] == "merged", res[0]["comment"]
    assert res[0]["flakes"] == ["T.BLK6_flaky"]
    reruns = [c for c in env.calls_list() if c["cmd"] == "test" and c["filter"]]
    assert [c["filter"] for c in reruns] == ["T.BLK6_flaky"] * 3
    posts = [c for c in env.calls_list() if c["cmd"] == "post"]
    assert len(posts) == 4                  # after the suite and after each solo rerun
    comment = env.tracker.moves[-1][2]
    assert "Known failures (allowed): T.CV5a_red" in comment
    assert "Flakes rerun (passed 3 solo reruns): T.BLK6_flaky" in comment


def test_a_flake_that_fails_solo_is_a_real_failure(env):
    env.config(tests={"T.A1_ok": "pass", "T.BLK6_bad": "fail"})
    p = env.project(flakes=["BLK6"])
    res = env.queue(p).merge(Ctx(), [env.task()])
    assert res[0]["result"] == "resume"
    assert res[0]["failing"] == ["T.BLK6_bad (flake: failed solo rerun 1/3)"]


def test_two_real_failures_in_a_row_block_the_task_and_name_the_tests(env):
    env.config(tests={"T.A1_ok": "pass", "T.A2_broken": "fail"})
    p = env.project()
    f = env.task()
    before = env.head()
    q = env.queue(p)
    r1 = q.merge(Ctx(), [f])[0]
    assert (r1["result"], r1["status"]) == ("resume", "In Progress")
    assert env.tracker.moves[-1][2].startswith("autopilot: resume.")
    assert "T.A2_broken" in env.tracker.moves[-1][2]
    r2 = q.merge(Ctx(), [f])[0]
    assert (r2["result"], r2["status"]) == ("blocked", "Blocked")
    assert "- T.A2_broken" in env.tracker.moves[-1][2] and "2 times in a row" in env.tracker.moves[-1][2]
    assert env.head() == before                 # nothing merged
    # a success resets the streak
    env.config(tests={"T.A1_ok": "pass"})
    assert q.merge(Ctx(), [f])[0]["result"] == "merged"
    st = json.loads((env.state_dir / "merge-state.json").read_text())
    assert st["projects"]["p"]["tasks"] == {}


def test_a_failing_compile_step_stops_the_gate(env):
    env.config(tests={"T.A1_ok": "pass"}, step_exit=2)
    res = env.queue(env.project()).merge(Ctx(), [env.task()])
    assert res[0]["result"] == "resume" and "exited 2" in res[0]["failing"][0]
    assert [c["cmd"] for c in env.calls_list()] == ["step"]


def test_a_test_count_drop_fails_the_gate(env):
    env.config(tests={"T.A1_ok": "pass", "T.A2_ok": "pass", "T.A3_ok": "pass"})
    p = env.project()
    q = env.queue(p)
    assert q.merge(Ctx(), [env.task()])[0]["result"] == "merged"
    env.config(tests={"T.A1_ok": "pass", "T.A2_ok": "pass"})
    res = q.merge(Ctx(), [env.task("2", content="x\n", fname="c.txt")])[0]
    assert res["result"] == "resume"
    assert res["failing"] == ["unit: test count dropped 3 -> 2"]


def test_a_suite_with_no_results_file_fails(env):
    env.config(tests={"T.A1_ok": "pass"})
    p = env.project()
    ap = p.autopilot
    silent = Command(f"python3 {env.bin}/fake_step.py suite")
    p = Project(id="p", path=p.path, autopilot=Autopilot(
        test={"unit": silent}, pre_merge_gate=(silent,), milestone_gate=(),
        worktree_root=ap.worktree_root, results="nunit"))
    res = env.queue(p).merge(Ctx(), [env.task()])[0]
    assert res["result"] == "resume" and "no results file" in res["failing"][0]


def test_a_project_without_a_gate_needs_the_operator(env):
    p = env.project(gate=())
    res = env.queue(p).merge(Ctx(), [env.task()])[0]
    assert (res["result"], res["status"]) == ("needs_operator", "Awaiting approval")


# ---- conflicts ---------------------------------------------------------------------------------

def test_a_conflict_is_aborted_and_handed_back_as_resume(env):
    p = env.project()
    f = env.task(content="one\nTASK\nthree\n")
    (env.repo / "a.txt").write_text("one\nMASTER\nthree\n")
    sh(env.repo, "commit", "-qam", "master edits the same line")
    branch_before = sh(f["worktree"], "rev-parse", "HEAD")
    master_before = env.head()
    q = env.queue(p)
    res = q.merge(Ctx(), [f])[0]
    assert (res["result"], res["status"], res["conflicts"]) == ("resume", "In Progress", ["a.txt"])
    comment = env.tracker.moves[-1][2]
    assert comment.startswith("autopilot: resume.") and "`a.txt`" in comment
    assert "git merge master" in comment
    assert sh(f["worktree"], "rev-parse", "HEAD") == branch_before
    assert not mq.merge_in_progress(f["worktree"])
    assert sh(f["worktree"], "status", "--porcelain", "--untracked-files=no") == ""
    assert env.head() == master_before
    assert env.calls_list() == []                   # no gate ran
    q.merge(Ctx(), [f])
    res = q.merge(Ctx(), [f])[0]
    assert (res["result"], res["status"]) == ("blocked", "Blocked")


# ---- the main checkout -------------------------------------------------------------------------

def test_dirty_main_checkout_needs_operator_and_the_edit_is_untouched(env):
    """AC7: a local edit the fast-forward would overwrite -> needs operator; same checksum."""
    p = env.project()
    f = env.task()
    (env.repo / "a.txt").write_text("operator's uncommitted edit\n")
    (env.repo / "scratch.txt").write_text("untracked\n")
    sums = {n: sha256(env.repo / n) for n in ("a.txt", "notes.txt", "scratch.txt")}
    status_before = sh(env.repo, "status", "--porcelain")
    before = env.head()
    res = env.queue(p).merge(Ctx(), [f])[0]
    assert (res["result"], res["status"]) == ("needs_operator", "Awaiting approval")
    comment = env.tracker.moves[-1][2]
    assert "a.txt" in comment and "Nothing was stashed" in comment
    assert {n: sha256(env.repo / n) for n in sums} == sums
    assert sh(env.repo, "status", "--porcelain") == status_before
    assert env.head() == before
    assert sh(env.repo, "stash", "list") == ""


def test_unrelated_local_edits_survive_a_merge(env):
    p = env.project()
    f = env.task()
    (env.repo / "notes.txt").write_text("operator editing notes\n")
    before = sha256(env.repo / "notes.txt")
    res = env.queue(p).merge(Ctx(), [f])[0]
    assert res["result"] == "merged"
    assert sha256(env.repo / "notes.txt") == before
    assert sh(env.repo, "status", "--porcelain") == "M notes.txt"


def test_main_checkout_on_another_branch_is_never_switched(env):
    p = env.project()
    f = env.task()
    sh(env.repo, "switch", "-q", "-c", "operator-work")
    res = env.queue(p).merge(Ctx(), [f])[0]
    assert res["result"] == "needs_operator" and "operator-work" in res["comment"]
    assert sh(env.repo, "branch", "--show-current") == "operator-work"
    assert sh(env.repo, "rev-parse", "master") == sh(env.repo, "rev-parse", "operator-work")


def test_default_moving_during_the_gate_merges_again(env):
    env.config(tests={"T.A1_ok": "pass"},
               hook=f"cd {env.tmp}/repo && echo new > late.txt && git add late.txt "
                    "&& git commit -qm late")
    p = env.project()
    res = env.queue(p).merge(Ctx(), [env.task()])[0]
    assert res["result"] == "merged"
    assert (env.repo / "late.txt").exists()
    assert len([c for c in env.calls_list() if c["cmd"] == "test"]) == 2


# ---- the queue itself --------------------------------------------------------------------------

def test_background_queue_is_serial_and_persists(env):
    p = env.project()
    f1 = env.task("1")
    f2 = env.task("2", content="x\n", fname="c.txt")
    q = mq.MergeQueue(project_of=lambda pid: p, tracker=env.tracker, state_dir=str(env.state_dir))
    ctx = Ctx()
    assert q.merge(ctx, [f1, f2]) == []
    q.wait("p", 60)
    assert [(t, s) for t, s, _ in env.tracker.moves] == [("1", "Done"), ("2", "Done")]
    assert q.busy("p") == []
    assert (env.repo / "c.txt").exists() and (env.repo / "a.txt").read_text().endswith("four\n")


def test_a_restart_resumes_the_persisted_queue(env):
    p = env.project()
    f = env.task(content="one\nTASK\nthree\n")
    state = mq.MergeState(str(env.state_dir / "merge-state.json"))
    state.project("p")["queue"].append({"task": "1", "branch": f["branch"],
                                        "worktree": f["worktree"], "queued": 0})
    state.save()
    # the old run died mid-merge: a MERGE_HEAD is left in the worktree
    (env.repo / "a.txt").write_text("one\nMASTER\nthree\n")
    sh(env.repo, "commit", "-qam", "m")
    subprocess.run(["git", "-C", f["worktree"], "merge", "master"], capture_output=True)
    assert mq.merge_in_progress(f["worktree"])
    q = env.queue(p)
    assert q.busy("p") == ["1"]
    res = q.merge(Ctx(), [])[0]
    assert res["result"] == "resume" and res["conflicts"] == ["a.txt"]
    assert not mq.merge_in_progress(f["worktree"])


def test_tracker_failure_does_not_stop_the_queue(env):
    class Broken:
        def move(self, *a):
            raise RuntimeError("OpenProject down")
    p = env.project()
    q = mq.MergeQueue(project_of=lambda pid: p, tracker=Broken(), state_dir=str(env.state_dir),
                      background=False)
    ctx = Ctx()
    res = q.merge(ctx, [env.task()])
    assert res[0]["result"] == "merged"
    assert any("OpenProject down" in m for k, m in ctx.events if k == "error")


def test_after_merge_hook_gets_the_result(env):
    seen = []
    q = env.queue(env.project(), after_merge=lambda ctx, r: seen.append(r["task"]))
    q.merge(Ctx(), [env.task()])
    assert seen == ["1"]


def test_a_missing_worktree_is_handed_back(env):
    res = env.queue(env.project()).merge(Ctx(), [{"task": "5", "outcome": "merge",
                                                  "branch": "feat/5-x", "worktree": "/nope"}])[0]
    assert res["result"] == "resume" and "missing" in res["comment"]


def test_dirty_checksums_lists_modified_and_untracked(env):
    (env.repo / "a.txt").write_text("changed\n")
    (env.repo / "new.txt").write_text("n\n")
    sums = mq.dirty_checksums(str(env.repo))
    assert set(sums) == {"a.txt", "new.txt"}
    assert sums["a.txt"] == sha256(env.repo / "a.txt")


# ---- the last result per task, for run mode "once" (#890) ---------------------------------------

def test_the_queue_keeps_each_tasks_last_result_across_a_restart(env):
    p = env.project()
    q = env.queue(p)
    assert q.result("p", "1") is None
    q.merge(Ctx(), [env.task("1")])
    q.merge(Ctx(), [{"task": "5", "outcome": "merge", "branch": "feat/5-x", "worktree": "/nope"}])
    assert q.result("p", "1") == "merged" and q.result("p", 5) == "resume"
    again = env.queue(p)                            # a restarted supervisor reads the file
    assert again.result("p", "1") == "merged" and again.result("p", "5") == "resume"
    assert again.busy("p") == []


def test_the_result_is_written_with_the_dequeue_and_only_the_last_are_kept(env, monkeypatch):
    monkeypatch.setattr(mq, "RESULTS_KEPT", 2)
    q = env.queue(env.project())
    for i, tid in enumerate(("7", "8", "9")):
        q.merge(Ctx(), [{"task": tid, "outcome": "merge", "branch": f"feat/{tid}-x",
                         "worktree": "/nope"}])
    st = json.loads((env.state_dir / "merge-state.json").read_text())["projects"]["p"]
    assert st["queue"] == [] and sorted(st["results"]) == ["8", "9"]
