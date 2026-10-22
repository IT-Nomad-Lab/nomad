"""Agent sessions for OpenProject mentions (docs/prd/op-mention-agent.md 5.4, task #919).

The operator mentions a bot on a work package; the engine calls POST /session here with a key
per work package (`op-<id>`). The first run for a key makes a worktree `<repo>-wt/op-<id>` on a
new branch `op/<id>-<slug>` from the default branch, and starts a Claude session there with a
fixed session id. Every later run for the key resumes that session in the same worktree, so the
agent keeps its memory of the task.

What a run may do is the dispatcher's build rule: edit files, never commit, push, merge or
switch branches. `plan` mode is read-only. Edits land in the task's worktree only; the
operator's main checkout is never touched.

State: one JSON record per key in SESSIONS_FILE (session id, project, worktree, branch, runs).
POST /session/close removes a clean worktree when the task closes and keeps a dirty one.

A run that ends on the Max usage limit (#962) answers {"ok": False, "limited": True, "reset_at",
"reset_clock", "session_id", ...}. The session counts as started, so the engine's rerun at the
reset resumes it with `--resume`. There is no paid fallback.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HUB not in sys.path:
    sys.path.insert(0, HUB)
from autopilot.usage_limit import clock, usage_limited  # noqa: E402  (shared with the worker)
SESSIONS_FILE = os.path.expanduser(os.environ.get(
    "NOMAD_SESSIONS_FILE", "~/.local/state/nomad-dispatch/sessions.json"))
SESSION_TIMEOUT = int(os.environ.get("NOMAD_SESSION_TIMEOUT", "1800"))
# Token budget (#1089): Sonnet unless NOMAD_DISPATCH_MODEL says otherwise, the 1M window off so a
# session compacts at 200K, subagents on Sonnet, no fast mode. The key blanked bills the Max plan.
MODEL = os.environ.get("NOMAD_DISPATCH_MODEL", "sonnet")
RUN_ENV = {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
           "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet", "CLAUDE_CODE_DISABLE_FAST_MODE": "1"}
KEY = re.compile(r"^op-([0-9]{1,9})$")
BOTS = ("nomad-dev", "nomad-manager", "nomad-research", "nomad-comms", "nomad-support",
        "nomad-ads")
# The nomad-pm tools a read-only run may call. A build run gets the whole server.
PM_READ = ("get", "search", "tree", "projects", "project_summary", "my_queue", "ready", "blocked")
NS = uuid.UUID("5f0c1e5a-6f1d-4c1e-9a55-6e6f6d61646f")      # fixed: the same key, the same id

SESSION_GUARD = (
    "You are one of NOMAD's agents, started because the operator mentioned you in a comment on "
    "an OpenProject work package. You are in a git worktree made for this work package, on its "
    "own branch `{branch}`. Read CLAUDE.md / README first for context and conventions. STRICT "
    "RULES: never run `git commit`, `git push`, `git merge`, `git rebase`, `git switch`, "
    "`git checkout <branch>` or any destructive or irreversible command. Leave every change "
    "uncommitted for the operator to review. Work only inside this worktree. The nomad-pm tools "
    "act as you ({bot}); keep the work package's status and comments true, as the SDLC says. "
    "End with a short summary for the operator: what you did or found, which files changed and "
    "why, and anything you need from them."
)
PLAN_SESSION_GUARD = (
    "You are one of NOMAD's agents, started because the operator mentioned you in a comment on "
    "an OpenProject work package. This run is READ-ONLY: do not edit any file. Investigate, "
    "answer or plan, grounded in this repo's CLAUDE.md / README and its actual code. End with a "
    "short answer for the operator."
)

_lock = threading.Lock()          # the state file
_busy = set()                     # keys with a run in flight


def session_id(key, n=0):
    return str(uuid.uuid5(NS, f"{key}:{n}"))


def _git(path, *args, timeout=60):
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                          timeout=timeout)


def _out(path, *args):
    try:
        p = _git(path, *args)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _changed(wt):
    """`git status --porcelain` lines, with their two status columns intact."""
    try:
        p = _git(wt, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in p.stdout.splitlines() if ln.strip()] if p.returncode == 0 else []


def load():
    try:
        with open(SESSIONS_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.replace(tmp, SESSIONS_FILE)


def _update(key, **fields):
    with _lock:
        d = load()
        rec = d.setdefault(key, {})
        rec.update(fields)
        _save(d)
        return dict(rec)


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40].strip("-") or "task"


def repo_top(path):
    """The repo's real top folder (symlinks resolved), or None if `path` is not in a git repo."""
    top = _out(os.path.realpath(path), "rev-parse", "--show-toplevel")
    return os.path.realpath(top) if top else None


def ensure_worktree(repo, key, title, default_branch):
    """(worktree, branch) for `key`, made on first use. Reuses the worktree if it is still there,
    and the branch if only the worktree was removed. Raises RuntimeError with the reason."""
    wp = KEY.match(key).group(1)
    wt = os.path.join(repo + "-wt", key)
    if os.path.isdir(wt) and _out(wt, "rev-parse", "--is-inside-work-tree") == "true":
        return wt, _out(wt, "branch", "--show-current")
    have = [b for b in _out(repo, "branch", "--list", f"op/{wp}-*",
                            "--format=%(refname:short)").splitlines() if b]
    branch = have[0] if have else f"op/{wp}-{_slug(title)}"
    if not have and not default_branch:
        raise RuntimeError(f"{repo} has no main or master branch to start from")
    os.makedirs(repo + "-wt", exist_ok=True)
    args = ["worktree", "add", wt, branch] if have else \
        ["worktree", "add", "-b", branch, wt, default_branch]
    p = _git(repo, *args)
    if p.returncode != 0:
        raise RuntimeError(f"could not make the worktree: {p.stderr.strip()[:300]}")
    return wt, branch


def preview(key, path, title):
    """What the next run for `key` in the project at `path` will use, without making anything:
    {session_id, branch, resumed}. The engine names them in its "on it" comment."""
    if not KEY.match(key or ""):
        return None
    rec = load().get(key) or {}
    repo = repo_top(path)
    branch = rec.get("branch")
    if repo and not branch:
        wp = KEY.match(key).group(1)
        have = [b for b in _out(repo, "branch", "--list", f"op/{wp}-*",
                                "--format=%(refname:short)").splitlines() if b]
        branch = have[0] if have else f"op/{wp}-{_slug(title)}"
    return {"session_id": rec.get("session_id") or session_id(key), "branch": branch,
            "resumed": bool(rec.get("runs") or rec.get("limited"))}


def mcp_config(bot, mcp_file=None):
    """The nomad-pm MCP server from the hub's .mcp.json, acting as `bot`."""
    try:
        with open(mcp_file or os.path.join(HUB, ".mcp.json")) as f:
            srv = json.load(f)["mcpServers"]["nomad-pm"]
    except (OSError, ValueError, KeyError):
        return None
    srv = {**srv, "env": {**srv.get("env", {}), "NOMAD_PM_AS": bot}}
    return json.dumps({"mcpServers": {"nomad-pm": srv}})


def claude_cmd(claude, prompt, sid, resume, mode, branch, bot, mcp):
    guard = PLAN_SESSION_GUARD if mode == "plan" else SESSION_GUARD.format(branch=branch, bot=bot)
    cmd = [claude, "-p", prompt, "--model", MODEL,
           *(["--resume", sid] if resume else ["--session-id", sid]),
           "--append-system-prompt", guard,
           "--permission-mode", "plan" if mode == "plan" else "acceptEdits",
           "--output-format", "json"]
    if mcp:
        tools = ",".join(f"mcp__nomad-pm__{t}" for t in PM_READ) if mode == "plan" \
            else "mcp__nomad-pm"
        cmd += ["--mcp-config", mcp, "--allowedTools", tools]
    return cmd


def _claude(cmd, cwd, runner, timeout):
    """(data, error, reset_at). reset_at is set when the run ended on the Max usage limit."""
    p = runner(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
               env={**os.environ, **RUN_ENV})
    try:
        data = json.loads(p.stdout or "{}")
    except ValueError:
        data = {}
    if p.returncode != 0 or data.get("is_error") or not data:
        err = data.get("result") or (p.stderr or p.stdout or "").strip()[:500]
        reset_at = usage_limited(data.get("result") or (p.stdout or "").strip(), p.stderr)
        return None, err or f"claude exited {p.returncode}", reset_at
    return data, None, None


def run(req, resolve, default_branch, claude, runner=subprocess.run, timeout=None):
    """POST /session. `resolve` and `default_branch` are the dispatcher's own."""
    key = str(req.get("key") or "")
    if not KEY.match(key):
        return {"ok": False, "error": "key must be op-<work package id>"}
    bot = req.get("bot") or "nomad-dev"
    if bot not in BOTS:
        return {"ok": False, "error": f"bot must be one of {', '.join(BOTS)}"}
    prompt = (req.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "empty prompt"}
    mode = "plan" if req.get("mode") == "plan" else "build"
    proj = resolve(req.get("project", ""))
    if not proj or proj.get("ambiguous"):
        return {"ok": False, "error": f"no single project {req.get('project')!r}"}
    repo = repo_top(proj["path"])
    if not repo:
        return {"ok": False, "error": f"{proj['name']} is not a git repo, so it cannot host a "
                                      "session (a session needs its own worktree)"}
    with _lock:
        if key in _busy:
            return {"ok": False, "busy": True, "error": f"{key} already has a run going"}
        _busy.add(key)
    try:
        rec = load().get(key) or {}
        if rec.get("repo") and rec["repo"] != repo:
            return {"ok": False, "error": f"{key} already has a session in {rec['repo']}"}
        wt, branch = ensure_worktree(repo, key, req.get("title") or prompt[:60],
                                     default_branch(repo))
        # A registry project in a subfolder of its repo runs in that subfolder of the worktree.
        rel = os.path.relpath(os.path.realpath(proj["path"]), repo)
        cwd = os.path.join(wt, rel) if rel != "." and os.path.isdir(os.path.join(wt, rel)) else wt
        sid = rec.get("session_id") or session_id(key)
        # A run cut off by the usage limit already started the session: the rerun resumes it.
        resume = bool(rec.get("runs") or rec.get("limited"))
        mcp = mcp_config(bot)
        t0 = time.time()
        data, err, reset_at = _claude(
            claude_cmd(claude, prompt, sid, resume, mode, branch, bot, mcp),
            cwd, runner, timeout or SESSION_TIMEOUT)
        if err and not reset_at and resume and "no conversation found" in err.lower():
            # The transcript is gone (cleared by hand): start a fresh session, same worktree.
            sid, resume = session_id(key, int(rec.get("restarts", 0)) + 1), False
            rec["restarts"] = int(rec.get("restarts", 0)) + 1
            data, err, reset_at = _claude(
                claude_cmd(claude, prompt, sid, False, mode, branch, bot, mcp),
                cwd, runner, timeout or SESSION_TIMEOUT)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        _update(key, session_id=sid, project=proj.get("slug") or proj["name"], repo=repo, bot=bot,
                worktree=wt, branch=branch, created=rec.get("created") or now, last_run=now,
                runs=int(rec.get("runs", 0)) + (0 if err else 1),
                restarts=int(rec.get("restarts", 0)), closed=False,
                limited=bool(reset_at) or (bool(rec.get("limited")) and bool(err)))
        out = {"session_id": sid, "resumed": resume, "worktree": wt, "branch": branch,
               "mode": mode, "secs": int(time.time() - t0),
               "diffstat": _out(wt, "diff", "--stat"),
               "changed": _changed(wt)}
        if reset_at:
            return {"ok": False, "limited": True, "reset_at": reset_at,
                    "reset_clock": clock(reset_at), "error": err, **out}
        if err:
            return {"ok": False, "error": err, **out}
        # total_cost_usd is what the run would have cost at API prices: a usage gauge, not a bill.
        return {"ok": True, "summary": data.get("result", ""), "cost_usd": data.get("total_cost_usd"),
                **out}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"the run timed out after {timeout or SESSION_TIMEOUT}s; the "
                                      "session is kept, so mention the bot again to continue"}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    finally:
        with _lock:
            _busy.discard(key)


def close(req):
    """POST /session/close {key}: the work package closed. A clean worktree is removed (the branch
    stays); one with changes is kept and its changes listed."""
    key = str(req.get("key") or "")
    if not KEY.match(key):
        return {"ok": False, "error": "key must be op-<work package id>"}
    rec = load().get(key)
    if not rec:
        return {"ok": True, "known": False}
    with _lock:
        if key in _busy:
            return {"ok": False, "busy": True, "error": f"{key} has a run going"}
    wt, repo = rec.get("worktree"), rec.get("repo")
    out = {"ok": True, "known": True, "worktree": wt, "branch": rec.get("branch"),
           "bot": rec.get("bot")}
    if wt and os.path.isdir(wt):
        dirty = _changed(wt)
        if dirty:
            _update(key, closed=True)
            return {**out, "removed": False, "changed": dirty}
        p = _git(repo, "worktree", "remove", wt)
        if p.returncode != 0:
            return {**out, "ok": False, "removed": False, "error": p.stderr.strip()[:300]}
    _update(key, closed=True)
    return {**out, "removed": True}


TAG = re.compile(r"\[([a-z0-9][a-z0-9._-]{0,63})\]", re.I)


def project_for(board, comment, subject, entries, board_for):
    """Which registry project a mention on `board` runs in (PRD 5.3, task #921).

    `entries` are the registry's (id, path) pairs, `board_for` maps an id to its board. Only git
    repos can host a session, so a board's candidates are its git projects. One candidate is the
    answer. With several, a `[project-id]` tag decides, looked for in the comment first, then in
    the subject. Returns {"project": id} or {"error": reason, "choices": [...]}."""
    on_board = [(pid, path) for pid, path in entries if board_for(pid) == board]
    hosts = [pid for pid, path in on_board if repo_top(path)]
    others = [pid for pid, _ in on_board if pid not in hosts]
    for text in (comment, subject):
        for tag in TAG.findall(text or ""):
            tag = tag.lower()
            if tag in hosts:
                return {"project": tag}
            if tag in others:
                return {"error": f"{tag} is not a git repo, so it cannot host a session",
                        "choices": hosts}
    if len(hosts) == 1:
        return {"project": hosts[0]}
    if hosts:
        return {"error": f"board {board} has several projects; say which with a tag",
                "choices": hosts}
    if others:
        return {"error": f"board {board}'s projects ({', '.join(others)}) are not git repos, "
                         "so none can host a session", "choices": []}
    return {"error": f"board {board} has no project to run in", "choices": []}
