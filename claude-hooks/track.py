#!/usr/bin/env python3
"""Session tracking hooks: tie a Claude Code session to its nomad-pm tasks (#1006).

docs/SDLC.md asks every agent to leave a status comment before a session ends. Agents spend turns
on that and still forget, so the harness does it:

  track.py start    SessionStart. If the branch is <type>/<id>-<slug>, show that task to the agent.
  track.py record   PostToolUse on Bash or PowerShell. After a git commit or merge, note repo,
                    branch and HEAD.
  track.py end      SessionEnd. Post one comment on each open task this session's commits name.

Run with the mcp-local venv (it imports nomad_pm from ~/nomad/mcp-local). Every failure is
silent: tracking must never block or slow a session down. State lives in
~/.local/state/nomad-track/<session>.jsonl and is removed once the summary is posted.

Windows sessions (#1051) reach this through bin/nomad-run, so it runs in WSL: the payload's
`cwd` and every `cd`/`git -C` target go through winpath.to_wsl, and git runs on the /mnt/c repo.
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tier_gate import git_sub, ps_prog, ps_segments, segments, strip_prefix  # noqa: E402
from winpath import on_windows, to_wsl  # noqa: E402

STATE = os.path.expanduser(os.environ.get("NOMAD_TRACK_STATE", "~/.local/state/nomad-track"))
PM_DIR = os.path.expanduser(os.environ.get("NOMAD_PM_DIR", "~/nomad/mcp-local"))
BRANCH_ID = re.compile(r"^[\w.-]+/(\d+)-")
MESSAGE_ID = re.compile(r"#(\d+)\b")
CLOSED = {"Done", "Won't do", "Duplicate", "Closed", "Rejected"}
RECORDED = {"commit", "merge", "pull", "cherry-pick", "revert"}
CD = {"cd", "chdir", "pushd", "set-location", "sl", "push-location"}   # the last three: PowerShell


# ── git ──

def git(d, *args):
    try:
        r = subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def branch_task(branch):
    m = BRANCH_ID.match(branch or "")
    return m.group(1) if m else None


def resolve(cwd, target, windows):
    """`target` (a cd or -C argument) from `cwd`, as a WSL path. A Windows session's `~` is its
    Windows home, which WSL can't name, so it is left as is and git then fails silently."""
    if not windows:
        target = os.path.expanduser(target)
    target = to_wsl(target, windows)
    if windows:
        target = target.replace("\\", "/")
    return os.path.join(cwd, target)


def git_targets(command, cwd, powershell=False, windows=False):
    """Directories in which `command` runs a recorded git subcommand (commit, merge, ...)."""
    out = []
    for seg in (ps_segments(command) if powershell else segments(command)) or []:
        seg = strip_prefix(seg)
        if powershell and len(seg) > 2 and seg[0].startswith("$") and seg[1] == "=":
            seg = seg[2:]                               # $out = git commit ...
        if not seg:
            continue
        prog = ps_prog(seg[0]) if powershell or windows else os.path.basename(seg[0])
        if prog in CD:
            args = [a for a in seg[1:] if not a.startswith("-")]
            if args:
                cwd = resolve(cwd, args[0], windows)
            continue
        if prog != "git":
            continue
        sub, _ = git_sub(seg)
        if sub not in RECORDED:
            continue
        d, i = cwd, 1
        while i < len(seg) and seg[i].startswith("-"):
            if seg[i] == "-C" and i + 1 < len(seg):
                d = resolve(d, seg[i + 1], windows)
                i += 2
            elif seg[i] in ("-c", "--git-dir", "--work-tree", "--namespace"):
                i += 2
            else:
                i += 1
        out.append(os.path.normpath(d))
    return out


# ── state ──

def state_path(session):
    return os.path.join(STATE, re.sub(r"[^\w.-]", "_", session or "unknown") + ".jsonl")


def record(data):
    tool = data.get("tool_name")
    if tool not in ("Bash", "PowerShell"):
        return
    command = (data.get("tool_input") or {}).get("command") or ""
    if not re.search(r"\bgit\b", command, re.I):
        return
    rows = []
    cwd = to_wsl(data.get("cwd") or os.getcwd())
    for d in git_targets(command, cwd, powershell=tool == "PowerShell", windows=on_windows()):
        sha = git(d, "rev-parse", "HEAD")
        top = git(d, "rev-parse", "--show-toplevel")
        if sha and top:
            rows.append({"repo": top, "branch": git(d, "symbolic-ref", "--short", "-q", "HEAD"),
                         "sha": sha, "ts": int(time.time())})
    if rows:
        os.makedirs(STATE, exist_ok=True)
        with open(state_path(data.get("session_id")), "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")


def load(session):
    try:
        with open(state_path(session)) as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


# ── summary ──

def commits_by_task(rows):
    """{task id: [(short sha, subject, branch, repo name)]}, each commit once, oldest first."""
    seen, tasks = set(), {}
    for r in rows:
        if r["sha"] in seen:
            continue
        seen.add(r["sha"])
        subject = git(r["repo"], "log", "-1", "--format=%s", r["sha"]) or ""
        ids = set(MESSAGE_ID.findall(subject))
        if not ids and branch_task(r.get("branch")):
            ids = {branch_task(r["branch"])}
        for tid in ids:
            tasks.setdefault(tid, []).append(
                (r["sha"][:7], subject, r.get("branch") or "detached", os.path.basename(r["repo"])))
    return tasks


def status_of(line):
    m = re.match(r"^#\d+ \[[^\]]+\] (.+?) · ", line or "")
    return m.group(1) if m else None


def summary(session, commits, status, reason):
    lines = [f"**Session end** (`{session[:8]}`, {reason or 'ended'}). Posted by the tracking hook."]
    lines += [f"- `{sha}` {subject} ({repo}, `{branch}`)" for sha, subject, branch, repo in commits]
    if status == "In Progress":
        lines.append("Still **In Progress**. If the work stopped, say where it stands and what comes next.")
    else:
        lines.append(f"Status: {status}.")
    return "\n".join(lines)


def end(data, pm):
    session = data.get("session_id") or "unknown"
    rows = load(session)
    if not rows:
        return []
    posted = []
    for tid, commits in sorted(commits_by_task(rows).items()):
        try:
            status = status_of(pm.get(tid).splitlines()[0])
        except Exception:
            continue                                    # no such task, or tracker down
        if not status or status in CLOSED:
            continue
        try:
            pm.comment(tid, summary(session, commits, status, data.get("reason")))
            posted.append(tid)
        except Exception:
            continue
    try:
        os.remove(state_path(session))
    except OSError:
        pass
    return posted


def start(data, pm):
    cwd = to_wsl(data.get("cwd") or os.getcwd())
    branch = git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    if branch is None:
        return None                                     # not a git repo, or detached
    tid = branch_task(branch)
    if tid:
        try:
            line = pm.get(tid).splitlines()[0]
        except Exception:
            return f"Branch `{branch}` names nomad-pm task #{tid}, but the tracker didn't answer."
        return (f"This session's branch `{branch}` is nomad-pm task {line}. Keep it current per "
                f"docs/SDLC.md; a session-end comment listing your commits is posted for you.")
    if branch in ("master", "main"):
        return None
    return (f"Branch `{branch}` has no task id. Name branches `<type>/<id>-<slug>` so the "
            f"tracking hook can link commits to their nomad-pm task.")


def pm_module():
    sys.path.insert(0, PM_DIR)
    import nomad_pm                                     # noqa: E402
    return nomad_pm


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    try:
        if mode == "record":
            record(data)
        elif mode == "start":
            ctx = start(data, pm_module())
            if ctx:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                         "additionalContext": ctx}}))
        elif mode == "end":
            end(data, pm_module())
    except Exception:
        pass                                            # never block a session on tracking


if __name__ == "__main__":
    main(sys.argv)
