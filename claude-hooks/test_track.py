"""Tests for track.py (#1006). Uses real throwaway git repos and a fake nomad_pm."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import track


def sh(d, *args):
    subprocess.run(["git", "-C", d, *args], check=True, capture_output=True)


class FakePM:
    def __init__(self, statuses):
        self.statuses, self.comments = statuses, []

    def get(self, tid):
        if tid not in self.statuses:
            raise RuntimeError("404")
        return f"#{tid} [Task] {self.statuses[tid]} · Some task · NOMAD work · lane dev · @Dev\nLane: dev"

    def comment(self, tid, text):
        self.comments.append((tid, text))
        return "ok"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        track.STATE = os.path.join(self.tmp.name, "state")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        sh(self.repo, "init", "-q", "-b", "master")
        sh(self.repo, "config", "user.email", "t@t")
        sh(self.repo, "config", "user.name", "t")
        self.commit("init")

    def commit(self, msg):
        sh(self.repo, "commit", "-q", "--allow-empty", "-m", msg)

    def bash(self, command, cwd=None, session="s1"):
        track.record({"session_id": session, "tool_name": "Bash", "cwd": cwd or self.repo,
                      "tool_input": {"command": command}})


class GitTargets(unittest.TestCase):
    def t(self, cmd, cwd="/w"):
        return track.git_targets(cmd, cwd)

    def test_plain_commit(self):
        self.assertEqual(self.t('git commit -m "x"'), ["/w"])

    def test_dash_C(self):
        self.assertEqual(self.t("git -C /r merge feat/x"), ["/r"])

    def test_relative_dash_C(self):
        self.assertEqual(self.t("git -C sub commit -m x"), ["/w/sub"])

    def test_cd_then_commit(self):
        self.assertEqual(self.t("cd /r && git add . && git commit -m x"), ["/r"])

    def test_dash_c_config_skipped(self):
        self.assertEqual(self.t("git -c user.name=a commit -m x"), ["/w"])

    def test_not_recorded(self):
        self.assertEqual(self.t("git status; git log; git diff"), [])

    def test_two_targets(self):
        self.assertEqual(self.t("git -C /a commit -m x; git -C /b merge y"), ["/a", "/b"])

    def test_heredoc_message(self):
        cmd = "git commit -F - <<'EOF'\nfix #12\n\ngit push later\nEOF"
        self.assertEqual(self.t(cmd), ["/w"])

    def test_branch_task(self):
        self.assertEqual(track.branch_task("feat/1006-tracking-hooks"), "1006")
        self.assertIsNone(track.branch_task("feat/tracking"))
        self.assertIsNone(track.branch_task(None))


class Record(Base):
    def test_records_commit(self):
        sh(self.repo, "checkout", "-q", "-b", "feat/7-x")
        self.commit("work")
        self.bash('git commit -m "work"')
        rows = track.load("s1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["branch"], "feat/7-x")
        self.assertEqual(os.path.realpath(rows[0]["repo"]), os.path.realpath(self.repo))

    def test_ignores_non_git(self):
        self.bash("ls -la")
        self.assertEqual(track.load("s1"), [])

    def test_ignores_other_tools(self):
        track.record({"session_id": "s1", "tool_name": "Edit", "cwd": self.repo,
                      "tool_input": {"command": "git commit"}})
        self.assertEqual(track.load("s1"), [])

    def test_missing_dir_is_silent(self):
        self.bash("git -C /nonexistent commit -m x")
        self.assertEqual(track.load("s1"), [])


class End(Base):
    def test_branch_id_posts_once_per_task(self):
        sh(self.repo, "checkout", "-q", "-b", "feat/7-x")
        self.commit("first")
        self.bash("git commit -m first")
        self.commit("second")
        self.bash("git commit -m second")
        self.bash("git status")                         # not recorded
        pm = FakePM({"7": "In Progress"})
        self.assertEqual(track.end({"session_id": "s1", "reason": "logout"}, pm), ["7"])
        tid, text = pm.comments[0]
        self.assertEqual(len(pm.comments), 1)
        self.assertIn("first", text)
        self.assertIn("second", text)
        self.assertIn("Still **In Progress**", text)
        self.assertEqual(track.load("s1"), [])         # state cleared

    def test_message_id_beats_branch(self):
        sh(self.repo, "checkout", "-q", "-b", "feat/7-x")
        self.commit("touch #9 and #10")
        self.bash("git commit -m y")
        pm = FakePM({"7": "In Progress", "9": "Ready", "10": "In Progress"})
        self.assertEqual(track.end({"session_id": "s1"}, pm), ["10", "9"])

    def test_closed_and_unknown_tasks_skipped(self):
        self.commit("for #3 and #4")
        self.bash("git commit -m x")
        pm = FakePM({"3": "Done"})                      # 4 does not exist
        self.assertEqual(track.end({"session_id": "s1"}, pm), [])
        self.assertEqual(pm.comments, [])

    def test_same_sha_recorded_twice_listed_once(self):
        self.commit("x #5")
        self.bash("git commit -m x")
        self.bash("git -C . merge nothing")            # HEAD unchanged
        pm = FakePM({"5": "In Progress"})
        track.end({"session_id": "s1"}, pm)
        self.assertEqual(pm.comments[0][1].count("x #5"), 1)

    def test_no_state_no_calls(self):
        pm = FakePM({})
        self.assertEqual(track.end({"session_id": "none"}, pm), [])

    def test_pm_comment_failure_is_silent(self):
        self.commit("x #5")
        self.bash("git commit -m x")

        class Broken(FakePM):
            def comment(self, tid, text):
                raise RuntimeError("down")
        self.assertEqual(track.end({"session_id": "s1"}, Broken({"5": "In Progress"})), [])


class Start(Base):
    def test_task_branch_shows_task(self):
        sh(self.repo, "checkout", "-q", "-b", "fix/42-bug")
        ctx = track.start({"cwd": self.repo}, FakePM({"42": "Ready"}))
        self.assertIn("#42 [Task] Ready", ctx)

    def test_tracker_down(self):
        sh(self.repo, "checkout", "-q", "-b", "fix/42-bug")
        self.assertIn("didn't answer", track.start({"cwd": self.repo}, FakePM({})))

    def test_untagged_branch_reminds(self):
        sh(self.repo, "checkout", "-q", "-b", "feat/thing")
        self.assertIn("<type>/<id>-<slug>", track.start({"cwd": self.repo}, FakePM({})))

    def test_default_branch_quiet(self):
        self.assertIsNone(track.start({"cwd": self.repo}, FakePM({})))

    def test_not_a_repo_quiet(self):
        self.assertIsNone(track.start({"cwd": self.tmp.name}, FakePM({})))


class PowerShellTargets(unittest.TestCase):
    """#1051: a Windows session's PowerShell tool, with paths seen from WSL under /mnt."""
    def t(self, cmd, cwd="/mnt/c/w"):
        return track.git_targets(cmd, cwd, powershell=True, windows=True)

    def test_plain_commit(self):
        self.assertEqual(self.t("git commit -m 'x'"), ["/mnt/c/w"])

    def test_set_location_then_commit(self):
        self.assertEqual(self.t(r"Set-Location C:\Users\josia\r; git commit -m x"),
                         ["/mnt/c/Users/josia/r"])

    def test_cd_alias_and_path_flag(self):
        self.assertEqual(self.t(r"cd -Path D:\r ; git merge y"), ["/mnt/d/r"])

    def test_relative_windows_dash_C(self):
        self.assertEqual(self.t(r"git -C sub\dir commit -m x"), ["/mnt/c/w/sub/dir"])

    def test_absolute_dash_C(self):
        self.assertEqual(self.t(r"git -C C:\x merge y"), ["/mnt/c/x"])

    def test_call_operator_full_path(self):
        self.assertEqual(self.t(r"& 'C:\Program Files\Git\cmd\git.exe' commit -m x"), ["/mnt/c/w"])

    def test_assignment(self):
        self.assertEqual(self.t("$out = git commit -m x"), ["/mnt/c/w"])

    def test_not_recorded(self):
        self.assertEqual(self.t("git status; Get-ChildItem"), [])


class GitBashTargets(unittest.TestCase):
    """#1051: a Windows session's Bash tool is Git Bash, whose /c/... means drive C."""
    def test_cd_drive_path(self):
        self.assertEqual(track.git_targets("cd /c/Users/r && git commit -m x", "/mnt/c/w",
                                           windows=True), ["/mnt/c/Users/r"])

    def test_git_exe(self):
        self.assertEqual(track.git_targets("git.exe -C D:/r commit -m x", "/mnt/c/w",
                                           windows=True), ["/mnt/d/r"])

    def test_linux_session_keeps_c(self):
        self.assertEqual(track.git_targets("cd /c/r && git commit -m x", "/w"), ["/c/r"])


class Windows(unittest.TestCase):
    """#1051 G7: a Windows cwd gives the same result as its /mnt/c path. NOMAD_WSL_MOUNT points
    the drive mount at a temp dir so the repo is real."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        track.STATE = os.path.join(self.tmp.name, "state")
        self.mount = os.path.join(self.tmp.name, "mnt")
        self.repo = os.path.join(self.mount, "c", "Users", "josia", "Projects", "bootalert")
        os.makedirs(self.repo)
        sh(self.repo, "init", "-q", "-b", "master")
        sh(self.repo, "config", "user.email", "t@t")
        sh(self.repo, "config", "user.name", "t")
        sh(self.repo, "checkout", "-q", "-b", "fix/42-bug")
        sh(self.repo, "commit", "-q", "--allow-empty", "-m", "work #42")
        env = mock.patch.dict(os.environ, {"NOMAD_WSL_MOUNT": self.mount, "NOMAD_HOST": "windows"})
        env.start()
        self.addCleanup(env.stop)

    def test_start_same_as_wsl_path(self):
        pm = FakePM({"42": "Ready"})
        win = track.start({"cwd": r"C:\Users\josia\Projects\bootalert"}, pm)
        self.assertIn("#42 [Task] Ready", win)
        self.assertEqual(win, track.start({"cwd": self.repo}, pm))
        self.assertEqual(win, track.start({"cwd": "C:/Users/josia/Projects/bootalert"}, pm))

    def test_powershell_record_and_end(self):
        track.record({"session_id": "w1", "tool_name": "PowerShell", "cwd": r"C:\Users\josia",
                      "tool_input": {"command": r"Set-Location Projects\bootalert; git commit -m 'work #42'"}})
        rows = track.load("w1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(os.path.realpath(rows[0]["repo"]), os.path.realpath(self.repo))
        self.assertEqual(rows[0]["branch"], "fix/42-bug")
        pm = FakePM({"42": "In Progress"})
        self.assertEqual(track.end({"session_id": "w1"}, pm), ["42"])
        self.assertIn("work #42", pm.comments[0][1])

    def test_git_bash_record(self):
        track.record({"session_id": "w2", "tool_name": "Bash", "cwd": r"C:\Users\josia",
                      "tool_input": {"command": "cd /c/Users/josia/Projects/bootalert && git commit -m x"}})
        self.assertEqual(len(track.load("w2")), 1)


class Main(Base):
    def run_hook(self, mode, payload):
        env = dict(os.environ, NOMAD_TRACK_STATE=track.STATE, NOMAD_PM_DIR="/nonexistent")
        r = subprocess.run(["python3", os.path.join(track.HERE, "track.py"), mode],
                           input=json.dumps(payload), capture_output=True, text=True, env=env)
        return r

    def test_bad_input_and_missing_pm_never_fail(self):
        for mode, payload in [("start", {"cwd": self.repo}), ("end", {"session_id": "s"}),
                              ("record", {"tool_name": "Bash", "tool_input": {}}), ("bogus", {})]:
            r = self.run_hook(mode, payload)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "")
        r = subprocess.run(["python3", os.path.join(track.HERE, "track.py"), "start"],
                           input="not json", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
