#!/usr/bin/env python3
"""NOMAD Builder dispatcher.

Runs headless Claude Code IN a target project's repo so the "Builder agent" can
actually implement a task there. Claude Code auto-reads the repo's CLAUDE.md, so
it works with full project context.

Endpoints (native WSL service; the console reaches it at host.docker.internal:8090):
  POST /dispatch {project, task, mode, branch?, plan?}
                                          mode = "plan" (read-only) | "build" (edits)
                                          plan = the approved plan, handed to the Builder
  POST /resolve {project}                 the registry id `project` names, or candidates;
                                          a folder or path inside a repo names that repo
  POST /create-project {name, prd, type?, …}
                                          set a whole project up: the engine's
                                          POST /owui/create-project, nomad_projects.create (#1374)
  GET  /create-project?id=                how that run is going
  POST /new-project {name, description, id?, prd?, stack?, lane?, local_only?, register?, template?}
                                          scaffold a project repo (new_project): one of
                                          create's steps, like the three below
  POST /seed-board {board, name}          add its nomad-pm board and seed it (seed_board)
  POST /register-project {id, local_only?, autopilot?, type?, policies?}
                                          add it to projects.yaml on a branch (register_hub)
  POST /project-stream {id}               its Zulip stream, via the relay (project_stream)
  POST /project-info {id}                 what deleting it would touch (project_info, #1200)
  POST /archive-project {id, confirm_remote?}
                                          move the repo to ~/archive/<id> (archive_project)
  POST /unregister-project {id}           drop it from projects.yaml on a branch (unregister_hub)
  POST /archive-board {board}             drop its seed.rb row, archive the board (archive_board)
  POST /archive-stream {id}               archive its Zulip stream, via the relay (archive_stream)
  POST /session {key, project, prompt, bot, mode, title}
                                          a resumable agent session per OpenProject work
                                          package, in its own worktree (sessions.py)
  POST /session/close {key}               the work package closed: drop a clean worktree
  POST /session/project {board, comment, subject, key?}
                                          the project a mention on that board runs in, and
                                          with `key`, the session and branch it will use
  GET  /projects                          discovered Nomad.md projects
  GET  /profiles                          the project types (profiles/*.yaml) for + New project
  GET  /profiles/list · /profiles/get?id=  POST /profiles/validate · /profiles/dry-run {id, yaml?}
                                          the cockpit's Standards area
                                          (nomad_projects/profile_api.py, #1377)
  POST /profiles/save {id, yaml}          commit a profile on branch profile/<id>, never
                                          master (#1379)
  GET  /policies · /policies/get?id= · /policies/effective?project=
                                          the policy layer (nomad_policy/api.py, #1396)
  POST /policies/simulate · /policies/tag-dry-run · /policies/tag-save · /policies/tag-accept · /policies/validate · /policies/save · /policies/attach
                                          simulate a call, preview a tag, save or attach
                                          on a policy/* branch, never on master
  POST /policies/guardrail-save {id, yaml, confirm}
                                          also needs X-Nomad-Cockpit (cockpit-token) and
                                          confirm = the id: the operator, from the cockpit
  POST /comfy/generate {graph, timeout?}  run one ComfyUI graph under shared GPU admission and
                                          return {ok, png_b64}, or busy (#1462, comfy_gate.py).
                                          The engine's local image provider goes through it
  GET  /health                            the only unauthenticated route

GUARDRAILS:
- Every route but /health requires the shared secret in an `X-Nomad-Token` header, the
  same header the n8n gate uses. The token is auto-generated at ~/.config/nomad/dispatch-token
  and read by the callers (containers mount that dir read-only). Without it this daemon
  lets anything on the host edit code in any marked repo.
- "build" runs with --permission-mode acceptEdits: Claude may EDIT files (reversible,
  uncommitted) but is instructed NEVER to git commit/push or run destructive commands.
- "build" never edits the default branch. If the repo is on master/main, the dispatcher first
  creates a feature branch (feat/<task-slug>) and the Builder works there; if it is already on
  another branch, that is the feature in progress and the Builder stays on it. `branch` names
  the branch to use instead, and `branch: "none"` is the operator's explicit opt-out.
  Merging back to the default branch stays human: only when the feature is done and tests pass.
- "plan" runs read-only (--permission-mode plan): no file changes at all.
- Only operates on dirs that carry a Nomad.md marker. Uses the local Claude Code login
  (subscription), never the Anthropic API key.
Review the working-tree diff before committing/pushing — commit/push stay human-gated.
"""
import base64
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import projects_registry  # noqa: E402  (shared with autopilot/projects.py)
import sessions  # noqa: E402  (POST /session, docs/prd/op-mention-agent.md)
import bridge_bind  # noqa: E402  (loopback + NOMAD_BRIDGE_IP, #383)
import comfy_gate  # noqa: E402  (POST /comfy/generate, shared GPU admission #1462)

HOST = os.environ.get("NOMAD_DISPATCH_HOST", "127.0.0.1")   # loopback only (#821); containers reach it on NOMAD_BRIDGE_IP (#383)
PORT = int(os.environ.get("NOMAD_DISPATCH_PORT", "8090"))
ROOTS = os.environ.get("PROJECT_ROOTS", "/home/jdelimam").split(":")
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")  # 2.x native
#   installer path, as in claude-bridge. The old linuxbrew one is gone, and every build failed on it
#   (#861). /health reports whether this exists, so the next move shows up in nomad-watch.
TIMEOUT = int(os.environ.get("NOMAD_DISPATCH_TIMEOUT", "900"))
# Token budget (#1089): Sonnet unless NOMAD_DISPATCH_MODEL says otherwise, the 1M window off so a
# run compacts at 200K, subagents on Sonnet, no fast mode. The key blanked bills the Max plan.
MODEL = os.environ.get("NOMAD_DISPATCH_MODEL", "sonnet")
RUN_ENV = {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
           "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet", "CLAUDE_CODE_DISABLE_FAST_MODE": "1"}


def claude_found() -> bool:
    """Whether the claude binary builds run with is there and runnable."""
    return os.path.isfile(CLAUDE) and os.access(CLAUDE, os.X_OK)
TOKEN_PATH = os.path.expanduser("~/.config/nomad/dispatch-token")


def _load_or_make_token():
    """The shared secret gating every route but /health. Auto-generated on first start, so
    there is nothing to configure; NOMAD_DISPATCH_TOKEN overrides it. Unlike termd this does
    NOT fail open: a dispatcher with no token is a remote-code-edit daemon with no door, so
    if we cannot establish one we exit and let systemd show the unit as failed."""
    env = os.environ.get("NOMAD_DISPATCH_TOKEN")
    if env:
        return env
    try:
        if os.path.exists(TOKEN_PATH):
            tok = open(TOKEN_PATH).read().strip()
            if tok:
                return tok
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        tok = secrets.token_urlsafe(24)
        with open(TOKEN_PATH, "w") as f:
            f.write(tok)
        os.chmod(TOKEN_PATH, 0o600)
        return tok
    except Exception as e:
        sys.exit(f"dispatcher: cannot read or create {TOKEN_PATH} ({type(e).__name__}: {e}). "
                 "Refusing to serve unauthenticated — set NOMAD_DISPATCH_TOKEN or fix the path.")


TOKEN = _load_or_make_token()

# The cockpit's own token (#1396). Only the engine sends it (X-Nomad-Cockpit), read from the
# /term-secret mount, and only for a guardrail save. A caller holding just the dispatch token, as
# agents do, cannot save a guardrail. The hooks deny agents this file and the route.
COCKPIT_TOKEN_PATH = os.path.expanduser("~/.config/nomad/cockpit-token")


def _cockpit_token():
    env = os.environ.get("NOMAD_COCKPIT_TOKEN")
    if env:
        return env
    try:
        if os.path.exists(COCKPIT_TOKEN_PATH):
            tok = open(COCKPIT_TOKEN_PATH).read().strip()
            if tok:
                return tok
        os.makedirs(os.path.dirname(COCKPIT_TOKEN_PATH), exist_ok=True)
        tok = secrets.token_urlsafe(24)
        fd = os.open(COCKPIT_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok)
        return tok
    except OSError:
        return ""                       # no token: every guardrail save is refused, nothing else changes


COCKPIT_TOKEN = _cockpit_token()

GUARD = (
    "You are NOMAD's Builder agent, working directly inside this repository. "
    "Read CLAUDE.md / README first for context and conventions, then implement the "
    "operator's request by editing files as needed, matching the existing style. "
    "STRICT RULES: never run `git commit`, `git push`, or any destructive/irreversible "
    "command; leave ALL changes uncommitted for human review. Never switch branches and "
    "never merge: the dispatcher has already put the repo on the right feature branch. "
    "When finished, give a "
    "concise summary of exactly which files you changed and why."
)
PLAN_GUARD = (
    "You are NOMAD's Builder agent. Produce a concrete, ordered implementation PLAN "
    "for the operator's request, grounded in this repo's CLAUDE.md/README and actual "
    "code. Do NOT edit any files — planning only."
)


def _frontmatter(d):
    """Parse the Nomad.md YAML-ish frontmatter into a lowercased-key dict."""
    meta = {}
    try:
        lines = open(os.path.join(d, "Nomad.md"), encoding="utf-8", errors="ignore").read().splitlines()
    except Exception:
        return meta
    if lines and lines[0].strip() == "---":
        for ln in lines[1:]:
            if ln.strip() == "---":
                break
            if ":" in ln:
                k, _, v = ln.partition(":")
                meta[k.strip().lower()] = v.strip()
    return meta


def _dir_slug(root, nm):
    """A repo's directory identifier. A root nested inside another root is a product group
    (~/civis holding poc/ and site/), so its repos are named `<group>-<dir>` ('civis-poc'):
    the bare dir name is generic ('app', 'site') and would false-match other projects."""
    parent = os.path.dirname(os.path.normpath(root))
    if any(os.path.normpath(r) == parent for r in ROOTS):
        return f"{os.path.basename(os.path.normpath(root))}-{nm}"
    return nm


REGISTRY_FILE = os.environ.get("NOMAD_REGISTRY", "")
_warned = set()


def _registry_file():
    """nomad/projects.yaml under the first root that has one (NOMAD_REGISTRY overrides).
    Its paths are relative to the folder holding nomad/, so the same file works on the host
    (~) and in the console container (/host)."""
    return projects_registry.registry_file(ROOTS, REGISTRY_FILE)


def load_projects():
    """Every entry in projects.yaml (dispatch: false included), with `path` made absolute.
    None when there is no registry or it cannot be parsed; callers then fall back to the scan."""
    f = _registry_file()
    if not f:
        return None
    try:
        entries = projects_registry.read_entries(f)
    except Exception as e:
        if f not in _warned:        # registry() runs per request; say it once
            _warned.add(f)
            print(f"dispatcher: cannot read {f} ({type(e).__name__}: {e}); scanning roots instead",
                  file=sys.stderr)
        return None
    return [{**e, "aliases": [str(a).lower() for a in e.get("aliases") or []],
             "dispatch": e.get("dispatch", True) is not False} for e in entries]


def _row(d, slug, also_extra=()):
    m = _frontmatter(d)
    name = m.get("name") or slug
    # `aliases: [a, b]` in Nomad.md: former names a repo still answers to exactly.
    also = [a.strip().strip("'\"").lower() for a in
            m.get("aliases", "").strip("[] ").split(",") if a.strip()]
    also = sorted({*also, *also_extra})
    aliases = {name.lower(), slug.lower(), slug.split("-")[0].lower(), *also}
    return {"name": name, "path": d, "slug": slug, "also": also,
            "lane": m.get("lane", ""), "repo": m.get("repo", ""),
            "status": (m.get("status") or "Active").capitalize(),
            "aliases": {a for a in aliases if len(a) >= 3}}


def _board_for():
    """autopilot/tasks.py's board_for: the BOARDS map, the hub being this file's parent."""
    hub = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if hub not in sys.path:
        sys.path.insert(0, hub)
    from autopilot import tasks
    return tasks.board_for


def projects_payload():
    """GET /projects: names, ids, and one detail row per dispatchable project."""
    reg = registry()
    board_for = _board_for()
    return {"projects": [r["name"] for r in reg],
            "ids": [r["slug"] for r in reg],
            "detail": [{"name": r["name"], "id": r["slug"], "lane": r["lane"], "repo": r["repo"],
                        "status": r["status"], "local_only": r.get("local_only", False),
                        "type": r.get("type", projects_registry.DEFAULT_TYPE),
                        "board": board_for(r["slug"])}
                       for r in reg]}


CONTEXT_MAX = int(os.environ.get("NOMAD_CONTEXT_MAX_CHARS") or 40000)


def _read_capped(path, limit):
    """{"path", "text", "chars", "truncated"} for a file, or None if it can't be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(limit + 1)
    except OSError:
        return None
    return {"path": path, "text": text[:limit], "truncated": len(text) > limit}


def project_context(pid):
    """GET /project-context?id=<id>: a dispatchable project's CLAUDE.md and PRD, read-only, for the
    Open WebUI sessions (docs/prd/owui-sessions.md §6.2). The PRD is docs/PRD.md, else the newest
    docs/prd/*.md. Looked up by registry id only, so no caller-supplied path is ever opened."""
    row = next((r for r in registry() if r["slug"] == pid), None)
    if row is None:
        return None
    root = row["path"]
    prd = os.path.join(root, "docs", "PRD.md")
    if not os.path.isfile(prd):
        d = os.path.join(root, "docs", "prd")
        try:
            cands = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md")]
        except OSError:
            cands = []
        prd = max(cands, key=os.path.getmtime) if cands else None
    def rel(c):
        return c and {**c, "path": os.path.relpath(c["path"], root)}
    return {"id": pid, "name": row["name"], "type": row.get("type", projects_registry.DEFAULT_TYPE),
            "board": _board_for()(pid),
            "claude_md": rel(_read_capped(os.path.join(root, "CLAUDE.md"), CONTEXT_MAX)),
            "prd": rel(prd and _read_capped(prd, CONTEXT_MAX))}


def registry():
    """The projects NOMAD may dispatch to. nomad/projects.yaml is the list when it exists:
    the entry id is the exact name, `dispatch: false` entries (publish targets, mirrors) are
    left out, and an entry whose folder is missing is skipped (the console cannot see /mnt/c).
    Without the file, fall back to scanning PROJECT_ROOTS one level deep for Nomad.md."""
    listed = load_projects()
    if listed is not None:
        out, seen = [], set()
        for e in listed:
            real = os.path.realpath(e["path"])
            if not e["dispatch"] or not os.path.isdir(e["path"]) or real in seen:
                continue
            seen.add(real)
            # local_only: true keeps the project's Open WebUI chats on local models (#1114).
            out.append({**_row(e["path"], e["id"], e["aliases"]), "local_only": e.get("local_only") is True,
                        "type": projects_registry.entry_type(e)})
        return out
    out, seen = [], set()
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for nm in sorted(os.listdir(root)):
            d = os.path.join(root, nm)
            real = os.path.realpath(d)
            if real in seen:            # the same repo reached through two roots or a symlink
                continue
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "Nomad.md")):
                seen.add(real)
                out.append(_row(d, _dir_slug(root, nm)))
    return out


def register_project(slug, path):
    """Append a scaffolded repo to projects.yaml, as text so the file's comments survive.
    No-op without a registry (the scan finds it) or when the id is already listed."""
    f = _registry_file()
    listed = load_projects()
    if not f or listed is None or any(e["id"] == slug for e in listed):
        return False
    rel = os.path.relpath(path, projects_registry.registry_base(f))
    body = open(f, encoding="utf-8").read()
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(("" if body.endswith("\n") else "\n") + f"  - {{id: {slug}, path: {rel}}}\n")
    return True


def resolve(project):
    """Find the one project a name refers to. Returns the row, or a dict with `ambiguous`
    listing the candidates. Two marked repos can carry the same name (a mirror, a second
    checkout), and this daemon EDITS code, so guessing between them writes changes into a
    repo nobody asked about. Ambiguity is reported, never resolved by listing order.

    Every tier is a whole-string match, case-insensitive. Exact wins first: the name, the
    registry id, a listed alias, or the `owner/repo` from Nomad.md. Then the short prefix
    aliases, so 'crypto' still finds crypto-trading-bot and 'nomad' hits every nomad-* repo;
    an exact match is the caller being specific and settles it. Only a tie inside one tier is
    a real ambiguity. Free text that merely CONTAINS an alias resolves to nothing (#851): the
    Manager once wrote 'fix-sshd-boot.ps1 (likely rogue01/NOMAD host repo)' and the old
    substring tier picked a repo out of that sentence for the Builder to edit.

    Last, a folder or path inside one repo names that repo (#906, see `_inside`), and the row
    carries it as `subdir` so the Builder is told where to start."""
    p = (project or "").strip().lower()
    if not p:
        return None
    reg = registry()
    exact = [r for r in reg if p in (r["name"].lower(), r["slug"].lower(), *r["also"])
             or ("/" in r["repo"] and p == r["repo"].lower())]
    loose = [r for r in reg if p in r["aliases"]]
    hits = [(r, "") for r in exact or loose] or _inside(project.strip(), reg)
    if not hits:
        return None
    if len(hits) > 1:
        return {"ambiguous": [{"name": r["name"], "id": r["slug"], "target": _target(r, sub),
                               "path": os.path.join(r["path"], sub) if sub else r["path"]}
                              for r, sub in hits]}
    r, sub = hits[0]
    return {**r, "subdir": sub} if sub else r


def _target(row, subdir):
    """How a resolved project is written back into a proposal: the registry id, plus the folder
    when the name was a part of the repo ('nomad/claude-bridge'). Resolves to the same row again."""
    return f"{row['slug']}/{subdir}" if subdir else row["slug"]


def _under(root, rel):
    """`rel` inside `root` as a normalised relative path, or None when it does not exist or climbs
    out of the repo ('../other-repo' must not reach a repo nobody named)."""
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, rel))
    if full != base and not full.startswith(base + os.sep) or not os.path.exists(full):
        return None
    return "" if full == base else os.path.relpath(full, base)


def _top_dirs(root):
    try:
        return [n for n in os.listdir(root) if not n.startswith((".", "_")) and n != "node_modules"
                and os.path.isdir(os.path.join(root, n))]
    except OSError:
        return []


def _inside(raw, reg):
    """The last tier: a part of a registered repo names that repo (#906). Run 50d61fe163ff sent
    'claude-bridge', a folder of the nomad repo, and failed three times after approval. Every form
    is checked against the disk, so it is a lookup, never a guess (#851):
      - a path, '/home/.../nomad/claude-bridge' or '~/nomad/claude-bridge', belongs to the repo
        holding it (the deepest one, should two ever nest);
      - '<project>/<path>' is that project, if the path exists inside it;
      - a bare name is a top-level folder, and more than one repo holding it is an ambiguity.
    Returns [(row, subdir)]."""
    if not raw:
        return []
    if raw.startswith(("/", "~")):
        full = os.path.realpath(os.path.expanduser(raw))
        if not os.path.exists(full):
            return []
        hits = [(r, sub) for r in reg if (sub := _under(r["path"], full)) is not None]
        deepest = max((len(os.path.realpath(r["path"])) for r, _ in hits), default=0)
        return [(r, s) for r, s in hits if len(os.path.realpath(r["path"])) == deepest]
    if "/" in raw:
        head, _, rest = raw.partition("/")
        h, rest = head.strip().lower(), rest.strip().strip("/")
        owners = [r for r in reg if h in (r["name"].lower(), r["slug"].lower(), *r["also"])]
        hits = [(r, sub) for r in owners if rest and (sub := _under(r["path"], rest))]
        return hits
    if any(c.isspace() for c in raw):
        return []
    return [(r, d) for r in reg for d in _top_dirs(r["path"]) if d.lower() == raw.lower()]


def candidates(project):
    """Registry ids to offer when `project` did not resolve. The repos whose alias appears inside
    the text come first, since that is usually what the caller meant; they are a suggestion for
    a human to pick from, never a match. With no such hint, every dispatchable id."""
    p = (project or "").strip().lower()
    reg = registry()
    near = [r["slug"] for r in reg if p and any(a in p for a in r["aliases"])]
    return near or [r["slug"] for r in reg]


def check_project(project):
    """What /resolve answers: the registry id a build would run in, or why there is none. The
    engine asks this while drafting a dispatch_build, so the gate can say the project is wrong
    before the operator approves rather than after (#851)."""
    proj = resolve(project)
    if proj and not proj.get("ambiguous"):
        sub = proj.get("subdir", "")
        return {"ok": True, "id": proj["slug"], "name": proj["name"], "path": proj["path"],
                "subdir": sub, "target": _target(proj, sub)}
    body = _unresolved(project, proj)
    body["reason"] = "ambiguous" if proj else "unknown"
    body["candidates"] = ([c["target"] for c in proj["ambiguous"]] if proj
                          else candidates(project))
    return body


def _unresolved(project, proj):
    """The error body for a name that matched nothing, or matched more than one repo."""
    if proj and proj.get("ambiguous"):
        where = ", ".join(f"{c['name']} at {c['path']}" for c in proj["ambiguous"])
        return {"ok": False, "error": f"'{project}' matches {len(proj['ambiguous'])} projects "
                f"({where}). Rename one Nomad.md marker, or ask for the exact directory name."}
    return {"ok": False, "error": f"Unknown project '{project}'. "
            f"Known: {[r['name'] for r in registry()]}"}


def default_branch(path):
    """The repo's default branch: origin's HEAD if known, else main or master if either exists."""
    head = git(path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head.split("/", 1)[-1]
    for b in ("main", "master"):
        if git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}"):
            return b
    return ""


def ensure_feature_branch(path, task, branch=None):
    """Put a build on a feature branch before any edit (operator rule: features never go straight
    onto the default branch). Returns (branch, None) or (None, error). Not a git repo, or the
    operator's explicit branch="none", leaves the checkout alone and returns ("", None)."""
    if branch == "none" or git(path, "rev-parse", "--is-inside-work-tree") != "true":
        return "", None
    current = git(path, "branch", "--show-current")
    if not current:
        return None, "Repo is on a detached HEAD; check out a branch first."
    if branch:
        if branch == current:
            return current, None
        exists = git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        args = ["switch", branch] if exists else ["switch", "-c", branch]
    else:
        if current != default_branch(path):
            return current, None  # a feature already in progress: keep building on it
        base = "feat/" + (_slug(" ".join(task.split()[:6]))[:40].strip("-") or "builder-task")
        branch, n = base, 2
        while git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            branch, n = f"{base}-{n}", n + 1
        args = ["switch", "-c", branch]
    proc = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return None, f"Could not switch to branch {branch}: {proc.stderr.strip()[:300]}"
    return branch, None


def builder_prompt(task, plan="", subdir=""):
    """The Builder's prompt. `task` is the one-line title; `plan` is what the operator actually
    approved at the gate. Before #851 the plan never left the engine, so the Builder improvised
    from the title and the approval covered text it never saw. `subdir` is the part of the repo
    the project was named by (#906): a pointer to where to start, not a fence, since the change
    may rightly land elsewhere in the repo."""
    if subdir:
        task = f"{task}\n\nThe request names `{subdir}` in this repository; start there."
    plan = (plan or "").strip()
    if not plan:
        return task
    return (f"{task}\n\nThe operator reviewed and approved this plan at NOMAD's gate. Implement "
            f"it as written; if the code makes a step wrong or impossible, stop and say so "
            f"instead of improvising.\n\nAPPROVED PLAN:\n{plan}")


def run_claude(path, task, mode, branch="", plan="", subdir=""):
    perm = "plan" if mode == "plan" else "acceptEdits"
    guard = PLAN_GUARD if mode == "plan" else GUARD
    if branch:
        guard += f" You are on feature branch `{branch}`."
    cmd = [CLAUDE, "-p", builder_prompt(task, plan, subdir), "--model", MODEL,
           "--append-system-prompt", guard,
           "--permission-mode", perm, "--output-format", "json"]
    proc = subprocess.run(cmd, cwd=path, capture_output=True, text=True,
                          timeout=TIMEOUT, env={**os.environ, **RUN_ENV})
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[:500]}")
    data = json.loads(proc.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude error: {data.get('result')}")
    return data.get("result", ""), data.get("total_cost_usd")


def git(path, *args):
    try:
        return subprocess.run(["git", "-C", path, *args], capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "project"


# Patterns the verify sandbox refuses — destructive, irreversible, or exfiltrating.
# Verification is meant to RUN tests/checks, not mutate state or reach the network.
_DENY = [
    r"\brm\b\s+-\w*[rf]", r"\bgit\s+(push|commit|reset|checkout|restore|clean|stash|rm|merge|rebase|cherry-pick)\b",
    r"\b(sudo|shutdown|reboot|halt|poweroff|mkfs|fdisk|parted|systemctl)\b",
    r"\bdd\b[^|]*\bof=", r":\(\)\s*\{", r">\s*/dev/(sd|nvme|disk)", r"/dev/tcp/",
    r"\bchown\b\s+-\w*R", r"\b(wget|nc|ncat|telnet|ssh|scp|rsync)\b",
    r"\bdocker\b", r"\b(kill|pkill|killall)\b", r"\bmv\b\s+\S+\s+/\b",
]
VERIFY_TIMEOUT = int(os.environ.get("NOMAD_VERIFY_TIMEOUT", "120"))


def guard_command(cmd):
    """Return an error string if `cmd` is unsafe to run in the verify sandbox, else None."""
    low = (cmd or "").lower()
    if not low.strip():
        return "Empty command."
    for pat in _DENY:
        if re.search(pat, low):
            return f"Blocked: command matches a forbidden pattern ({pat}). Verify only RUNS checks."
    if re.search(r"\bcurl\b", low) and not re.search(r"localhost|127\.0\.0\.1", low):
        return "Blocked: curl is restricted to localhost/127.0.0.1 (no external network)."
    return None


def verify(project, command):
    """Run a read-only verification command inside a project repo, return rc + output."""
    proj = resolve(project)
    if not proj or proj.get("ambiguous"):
        return _unresolved(project, proj)
    err = guard_command(command)
    if err:
        return {"ok": False, "error": err}
    t0 = time.time()
    try:
        p = subprocess.run(command, cwd=proj["path"], shell=True, capture_output=True,
                           text=True, timeout=VERIFY_TIMEOUT,
                           env={**os.environ, "GIT_PAGER": "cat", "PAGER": "cat"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Verify timed out after {VERIFY_TIMEOUT}s."}
    return {"ok": True, "project": proj["name"], "rc": p.returncode,
            "stdout": p.stdout[-4000:], "stderr": p.stderr[-2000:],
            "secs": round(time.time() - t0, 1)}


SHARED_MCP = ("nomad-local", "nomad-scrape", "nomad-image", "nomad-detect", "nomad-audio")
OWNER = os.environ.get("NOMAD_OWNER", "Josias")
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
SHARED_NOTE = ("Shared tools and services (MCP servers, NOMAD CRM, AI-text detection, Codex, local "
               "image and\nmusic generation, Penpot) are documented once in "
               "`~/nomad/docs/SHARED-RESOURCES.md`. Use them;\ndon't modify them here (extend the "
               "platform in the `nomad` repo).\n")


def _scaffold_files(name, slug, desc, prd, stack, lane, local_only, today):
    """What a new project repo starts with (docs/prd/owui-sessions.md §6.4 step 3.1): what an
    existing project has, so every NOMAD surface that reads Nomad.md, CLAUDE.md, AGENTS.md or
    .mcp.json sees it like the others."""
    hub = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    py = os.path.join(hub, "mcp-local", ".venv", "bin", "python")
    mcp = {"mcpServers": {s: {"command": py,
                              "args": [os.path.join(hub, "mcp-local", s.replace("-", "_") + ".py")]}
                          for s in SHARED_MCP}}
    stack_line = "[" + ", ".join(stack) + "]"
    sdlc = ("How to work (autonomy tiers, branches, PRD, nomad-pm tasks): `~/nomad/docs/SDLC.md`. "
            "Read it before planning or starting work.")
    prd_note = "The approved PRD is `docs/PRD.md`. " if prd else "Write the PRD first: `docs/PRD.md`. "
    local = ("\n- This project is **local-only**: its Open WebUI chats use local models, never a "
             "cloud model.") if local_only else ""
    files = {
        "Nomad.md": (f"---\nnomad: true\nname: {name}\nstatus: planning\nowner: {OWNER}\n"
                     f"repo: \"\"\nlane: {lane}\nstack: {stack_line}\nupdated: {today}\n---\n"
                     f"# {name}\n\n{desc}\n\n{SHARED_NOTE}\n"
                     "> Managed by **NOMAD**. Companion files: **CLAUDE.md** (context) · "
                     "**AGENTS.md** (agent build/test rules) · **docs/PRD.md** (the PRD).\n"),
        "CLAUDE.md": (f"# {name}: context for AI coding agents\n\n> NOMAD-managed. Created {today}."
                      f"\n\n## What it is\n{desc}\n\n## Status\nPlanning. {prd_note}The roadmap "
                      f"and tasks are on the `{slug}` board in nomad-pm.\n\n## How to work\n{sdlc}"
                      "\n\n## Conventions\n- Secrets live in `.env`: never committed, never "
                      "printed.\n- Each piece of work gets its own branch off the default branch."
                      "\n- Build and test rules: `AGENTS.md`." + local + "\n"),
        "AGENTS.md": (f"# AGENTS.md: build and guardrail rules for {slug}\n\n## Golden rules\n"
                      f"- {sdlc}\n- Every piece of work has a nomad-pm task on the `{slug}` board, "
                      "and its status matches what really happened.\n- Secrets stay in `.env`; "
                      "never commit them.\n- Use model role aliases, never raw model names.\n\n"
                      "## Run / test (before calling a change done)\nNot set up yet: the first "
                      "milestone adds the test command here and in `~/nomad/projects.yaml`.\n\n"
                      "## Publish (gated: human approval, via NOMAD)\nDeploying, exposing "
                      "externally, sending email, pushing, or any spend goes through NOMAD's human "
                      "approval gate.\n\n## Git workflow (hard rule)\n- Develop each feature on its "
                      "own branch, created from the default branch before the first edit.\n- Merge "
                      "into the default branch only when the feature is done and all tests pass.\n"
                      "- Never force-push.\n"),
        "README.md": f"# {name}\n\n{desc}\n\n_Scaffolded by NOMAD on {today}._\n",
        ".gitignore": ".env\n.env.*\n!.env.example\n__pycache__/\n*.pyc\nnode_modules/\n.venv/\n.DS_Store\n",
        ".mcp.json": json.dumps(mcp, indent=2) + "\n",
    }
    if prd:
        files["docs/PRD.md"] = prd.rstrip() + "\n"
    return files


TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _template_files(template):
    """Files of templates/<template>/ as {relative path: text}; {} when there is none."""
    root = os.path.join(TEMPLATES, template or "")
    out = {}
    for dp, _, fns in os.walk(root) if template and os.path.isdir(root) else ():
        for fn in fns:
            fp = os.path.join(dp, fn)
            with open(fp, encoding="utf-8") as f:
                out[os.path.relpath(fp, root)] = f.read()
    return out


def new_project(name, description="", id="", prd="", stack=(), lane="dev", local_only=False,
                register=True, template=""):
    """Scaffold a new NOMAD project repo under the first PROJECT_ROOTS dir: Nomad.md, CLAUDE.md,
    AGENTS.md, .mcp.json, README, .gitignore and, given `prd`, docs/PRD.md; then git init and the
    first commit. Returns {ok, name, slug, path}.

    The console's form sends only `name` and `description`: the repo is also appended to
    projects.yaml, and an existing folder is an error. The Open WebUI + New project setup
    (owui_setup, §6.4) sends `id` and `register: false`, registers on a hub branch itself, and
    re-runs a step after a failure, so an existing repo with that id's Nomad.md is a success
    (`existed: true`), left as it is."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "Project needs a name."}
    slug = (id or "").strip() or _slug(name)
    if not PROJECT_ID.match(slug):
        return {"ok": False, "error": f"'{slug}' is not a usable project id."}
    path = os.path.join(ROOTS[0], slug)
    if os.path.exists(path):
        if id and os.path.isfile(os.path.join(path, "Nomad.md")) and git(path, "rev-parse", "HEAD"):
            return {"ok": True, "name": name, "slug": slug, "path": path, "existed": True}
        return {"ok": False, "error": f"'{slug}' already exists at {path}."}
    today = date.today().isoformat()
    desc = (description or "").strip() or f"{name}, a NOMAD-managed project."
    stack = [str(x).strip() for x in (stack or []) if str(x).strip()]
    files = _scaffold_files(name, slug, desc, prd or "", stack, lane or "dev", bool(local_only), today)
    files.update(_template_files(template))   # e.g. "website": .claude/settings.json allowlist
    if template == "website":
        files["AGENTS.md"] += ("\n## Website standards\nStatic export, design source of truth, quality "
                              "gates and launch checklist: `~/nomad/docs/standards/WEBSITES.md` and "
                              "`docs/WEBSITE-CHECKLIST.md`. Read them before the first milestone.\n")
        files["CLAUDE.md"] += "\n## Website\nBuilt to `~/nomad/docs/standards/WEBSITES.md`.\n"
    for fn, body in files.items():
        fp = os.path.join(path, fn)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(body)
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "-c", "user.email=nomad@itnomadlab.com", "-c", "user.name=NOMAD",
        "commit", "-q", "-m", f"Scaffold {name} (NOMAD)")
    if not git(path, "rev-parse", "HEAD"):
        return {"ok": False, "error": f"git init or the first commit failed in {path}."}
    if register:
        register_project(slug, path)
    return {"ok": True, "name": name, "slug": slug, "path": path}


# ── nomad-pm board for a new project (POST /seed-board, §6.4 step 3.3) ──────────────────
# Seeding runs `docker exec` into the OpenProject container, so it happens here on the host.
# ~/nomad-pm is a shared checkout someone may be editing: the board row is added on its own
# branch in a worktree, seeded from there, and fast-forwarded into main only when git can.
PM_REPO = os.environ.get("NOMAD_PM_REPO", os.path.expanduser("~/nomad-pm"))
PM_BRANCH = os.environ.get("NOMAD_PM_BRANCH", "main")
SEED_TIMEOUT = int(os.environ.get("NOMAD_SEED_TIMEOUT", "900"))
_BOARD_ROW = re.compile(r'\["([a-z0-9-]+)",\s*"[^"]*"\]')


def _boards(seed_rb):
    m = re.search(r"^BOARDS = \[\n(.*?)^\]\.freeze", seed_rb, re.S | re.M)
    return (m, _BOARD_ROW.findall(m.group(1))) if m else (None, [])


def _add_board(seed_rb, ident, name):
    m, idents = _boards(seed_rb)
    if not m:
        raise ValueError("seed.rb has no BOARDS = [ ... ].freeze block")
    if ident in idents:
        return seed_rb
    body = m.group(1).rstrip("\n")
    return seed_rb[:m.start(1)] + f'{body},\n  ["{ident}", "{name}"]\n' + seed_rb[m.end(1):]


def _run(cmd, cwd, timeout=30):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "GIT_PAGER": "cat"})
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def seed_board(ident, name):
    """Add [ident, name] to BOARDS in ~/nomad-pm/seed.rb and run ./seed.sh. Re-running is safe:
    a board already in main is only re-seeded. Returns {ok, board, op_id, merged, branch, note}."""
    ident, name = (ident or "").strip(), (name or "").strip()
    if not PROJECT_ID.match(ident):
        return {"ok": False, "error": f"'{ident}' is not a usable board identifier."}
    if not name or any(c in name for c in '"\\#\n'):
        return {"ok": False, "error": "The board name can't be empty or hold \", \\, # or a newline."}
    rc, main_rb = _run(["git", "show", f"{PM_BRANCH}:seed.rb"], PM_REPO)
    if rc:
        return {"ok": False, "error": f"can't read seed.rb on {PM_BRANCH} in {PM_REPO}"}
    branch, wt = f"board/{ident}", f"{PM_REPO}-wt/board-{ident}"
    if ident in _boards(main_rb)[1]:
        where, branch = PM_REPO, ""
    else:
        why = _worktree(PM_REPO, wt, branch, PM_BRANCH)
        if why:
            return {"ok": False, "error": why}
        seed = os.path.join(wt, "seed.rb")
        text = open(seed, encoding="utf-8").read()
        new = _add_board(text, ident, name)
        if new != text:
            with open(seed, "w", encoding="utf-8") as f:
                f.write(new)
        why = _commit(wt, ["seed.rb"], f"Board {ident}: {name} (NOMAD + New project)")
        if why:
            return {"ok": False, "error": why}
        where = wt
    try:
        rc, out = _run(["bash", os.path.join(where, "seed.sh")], where, timeout=SEED_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"seed.sh timed out after {SEED_TIMEOUT}s", "branch": branch}
    m = re.search(rf"board: {re.escape(ident)} id=(\d+)", out)
    if rc or not m:
        return {"ok": False, "error": f"seed.sh failed (rc {rc}): {out[-500:]}", "branch": branch}
    res = {"ok": True, "board": ident, "op_id": int(m.group(1)), "merged": not branch,
           "branch": branch, "note": ""}
    if branch:
        why = _ff_into(PM_REPO, branch, PM_BRANCH)
        if why:
            res["note"] = f"The board is seeded. Branch {branch} isn't merged into {PM_BRANCH} yet: {why}"
        else:
            res["merged"] = True
            _drop(PM_REPO, wt, branch, PM_BRANCH)
    return res


def _ff_into(repo, branch, target):
    """Fast-forward `target` to `branch` without touching anyone's work. Where `target` is
    checked out, `git merge --ff-only` runs there, and git refuses when local edits are in the
    way. Where it isn't checked out (another session switched the shared checkout), the ref
    moves only if it is an ancestor. Returns "" when merged, else why not."""
    rc, out = _run(["git", "worktree", "list", "--porcelain"], repo)
    where, cur = None, None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = line[9:]
        elif line == f"branch refs/heads/{target}":
            where = cur
    if where:
        rc, out = _run(["git", "merge", "--ff-only", "-q", branch], where)
        if rc == 0:
            return ""
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if any("local changes" in ln for ln in lines):
            return f"{where} has uncommitted changes to {', '.join(ln for ln in lines if not ln.endswith(('merge:', '.', 'Aborting')))}"
        return lines[0][:200] if lines else "git merge --ff-only failed"
    if _run(["git", "merge-base", "--is-ancestor", target, branch], repo)[0]:
        return f"{target} has moved on; merge {branch} by hand"
    old = _run(["git", "rev-parse", target], repo)[1].strip()
    new = _run(["git", "rev-parse", branch], repo)[1].strip()
    rc, out = _run(["git", "update-ref", f"refs/heads/{target}", new, old], repo)
    return "" if rc == 0 else out.strip()[-200:]


def _drop(repo, wt, branch, target):
    """Remove a merged work branch and its worktree. `branch -d` judges "merged" against HEAD,
    which may be another session's branch, so the check is against `target` instead."""
    _run(["git", "worktree", "remove", wt], repo)
    if _run(["git", "merge-base", "--is-ancestor", branch, target], repo)[0] == 0:
        _run(["git", "branch", "-D", branch], repo)


def _worktree(repo, wt, branch, base):
    """A worktree on `branch` (made from `base` if new), or why it can't be made."""
    if os.path.isdir(wt):
        return ""
    have = _run(["git", "rev-parse", "--verify", "-q", branch], repo)[0] == 0
    rc, out = _run(["git", "worktree", "add", "-q", wt, *([branch] if have else ["-b", branch, base])],
                   repo)
    return f"git worktree add failed: {out[-300:]}" if rc else ""


def _commit(wt, paths, message):
    _run(["git", "add", "--", *paths], wt)
    if _run(["git", "diff", "--cached", "--quiet"], wt)[0] == 0:
        return ""                                   # nothing new: an earlier run committed it
    rc, out = _run(["git", "-c", "user.email=nomad@itnomadlab.com", "-c", "user.name=NOMAD",
                    "commit", "-q", "-m", message], wt)
    return f"commit failed: {out[-300:]}" if rc else ""


# ── hub registry for a new project (POST /register-project, §6.4 step 3.2) ───────────────
HUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB_BRANCH = os.environ.get("NOMAD_HUB_BRANCH", "master")
NO_TESTS = "echo 'No test command yet: set it in projects.yaml' >&2; exit 1"
PY = ["uv", "run", "--no-project", "--with", "pytest", "--with", "pyyaml", "--with", "requests",
      "python"]


def _registry_entry(pid, rel, local_only, autopilot, ptype="", policies=(), gates=None):
    """The projects.yaml entry. `ptype` and `policies` are left out when empty, so an untyped
    project's entry is what it was before types (#1373). `gates` is the profile's
    {pre_merge, milestone}: written into the autopilot block, a step list as JSON strings."""
    lines = [f"  - id: {pid}", f"    path: {rel}"]
    if ptype and ptype != projects_registry.DEFAULT_TYPE:
        lines.append(f"    type: {ptype}")
    if policies:
        lines.append(f"    policies: [{', '.join(policies)}]")
    if local_only:
        lines.append("    local_only: true")
    if autopilot:
        lines += ["    autopilot:                       # + New project; the switch starts off",
                  f'      test: "{NO_TESTS}"   # the first milestone sets the real one']
        for key, k in (("pre_merge", "pre_merge_gate"), ("milestone", "milestone_gate")):
            steps = (gates or {}).get(key) or ()
            if steps:
                lines.append(f"      {k}: [{', '.join(json.dumps(x) for x in steps)}]"
                             f"   # from the {ptype} profile")
    return "\n".join(lines) + "\n"


def _profiles():
    """nomad_projects.profiles from this checkout (the hub)."""
    if HUB not in sys.path:
        sys.path.insert(0, HUB)
    from nomad_projects import profiles
    return profiles


def profiles_payload():
    """GET /profiles: every project type, generic last, for the + New project type question
    (#1373). A bad profile file is an error, not a shorter list."""
    try:
        got = _profiles().load_all()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    order = sorted(got, key=lambda t: (t == projects_registry.DEFAULT_TYPE, got[t].title.lower()))
    return {"ok": True, "default": projects_registry.DEFAULT_TYPE, "profiles": [
        {"id": t, "title": got[t].title, "description": got[t].description,
         "interview": {"ask": [dict(q, options=list(q.get("options", ())))
                               if "options" in q else dict(q) for q in got[t].interview["ask"]],
                       "skip": list(got[t].interview["skip"])},
         "stack": got[t].stack, "policies": list(got[t].policies),
         "agents": [dict(a) for a in got[t].agents],
         "gates": {k: list(v) for k, v in got[t].gates.items()}} for t in order]}


# ── policies (#1396): the cockpit's Policies area, nomad_policy/api.py on this checkout ─────
POLICY_GETS = ("/policies", "/policies/get", "/policies/effective")
POLICY_POSTS = ("/policies/simulate", "/policies/tag-dry-run", "/policies/tag-save", "/policies/tag-accept",
                "/policies/validate", "/policies/save", "/policies/attach", "/policies/guardrail-save")


def _policy_api():
    if HUB not in sys.path:
        sys.path.insert(0, HUB)
    from nomad_policy import api
    return api


def policies_route(route, args, cockpit=False):
    """(status, body) for one /policies route. `args` is the query (GET) or the JSON body (POST).
    `cockpit` is true only when the request carried the cockpit token."""
    api = _policy_api()

    def s(k):
        v = args.get(k)
        return v if isinstance(v, str) else ""
    try:
        if route == "/policies":
            return 200, api.list_policies()
        if route == "/policies/get":
            return 200, api.get_policy(s("id"))
        if route == "/policies/effective":
            return 200, api.effective(s("project"))
        if route == "/policies/simulate":
            return 200, api.simulate(s("project"), s("kind"), s("name"), args.get("context") or {})
        if route == "/policies/tag-dry-run":
            return 200, api.tag_dry_run(s("project"), s("type"))
        if route == "/policies/tag-save":
            return 200, api.tag_save(s("project"), s("type"))
        if route == "/policies/tag-accept":
            return 200, api.tag_accept(s("project"), s("change"))
        if route == "/policies/validate":
            return 200, api.validate(s("id"), s("yaml"))
        if route == "/policies/save":
            return 200, api.save(s("id"), s("yaml"))
        if route == "/policies/attach":
            return 200, api.attach(s("target"), s("name"), s("policy"), detach=args.get("detach") is True)
        if route == "/policies/guardrail-save":
            if not cockpit:
                return 403, {"ok": False, "error": "a guardrail is saved only from the cockpit, by the operator"}
            return 200, api.save_guardrail(s("id"), s("yaml"), args.get("confirm"))
    except api.ApiError as e:
        return e.status, {"ok": False, "error": str(e)}
    except Exception as e:
        return 500, {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    return 404, {"ok": False, "error": "not found"}


# ── profiles (#1377): the cockpit's Standards area, nomad_projects/profile_api.py ───────────
PROFILE_GETS = ("/profiles/list", "/profiles/get", "/profiles/dry-run")   # GET dry-run: master's version (#1378)
PROFILE_POSTS = ("/profiles/validate", "/profiles/dry-run", "/profiles/save")   # save: on profile/<id> (#1379)


def profiles_route(route, args):
    """(status, body) for one /profiles/* route. `args` is the query or the JSON body. The only
    write is save, which commits on branch profile/<id> and never on master."""
    _policy_api()
    from nomad_projects import profile_api as api

    def s(k):
        v = args.get(k)
        return v if isinstance(v, str) else ""
    try:
        if route == "/profiles/list":
            return 200, api.list_profiles()
        if route == "/profiles/get":
            return 200, api.get_profile(s("id"))
        if route == "/profiles/validate":
            return 200, api.validate(s("id"), s("yaml"), keep=args.get("keep") is True)
        if route == "/profiles/dry-run":
            return 200, api.dry_run(s("id"), s("yaml") if s("yaml").strip() else None)
        if route == "/profiles/save":
            return 200, api.save(s("id"), s("yaml"), keep=args.get("keep") is True)
    except api.ApiError as e:
        return e.status, {"ok": False, "error": str(e)}
    except Exception as e:
        return 500, {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    return 404, {"ok": False, "error": "not found"}


def _type_and_policies(ptype, policies):
    """(profile or None, policy ids, error). The type must have a profile; each attached
    policy needs a policies/types/<id>.yaml. Nothing the caller sends is written unchecked."""
    ptype = (ptype or "").strip() or projects_registry.DEFAULT_TYPE
    prof = None
    try:
        profiles = _profiles()
        if not profiles.ID_RE.match(ptype):
            return None, (), f"'{ptype}' is not a project type"
        prof = profiles.load(ptype)
        ids = []
        for x in policies or ():
            x = str(x).strip()
            if not profiles.ID_RE.match(x) or not os.path.isfile(
                    os.path.join(profiles.TYPE_POLICIES_DIR, x + ".yaml")):
                return None, (), f"no policy '{x}' in policies/types/"
            if x not in ids:
                ids.append(x)
    except Exception as e:
        return None, (), f"type '{ptype}': {type(e).__name__}: {str(e)[:200]}"
    return prof, tuple(ids), ""


def _folders(path):
    try:
        return len(json.load(open(path, encoding="utf-8")).get("folders") or [])
    except (OSError, ValueError, AttributeError):
        return 0


def register_hub(pid, local_only=False, autopilot=False, ptype="", policies=()):
    """Add a scaffolded project to ~/nomad/projects.yaml on a `register/<id>` branch: the entry
    (with an autopilot block when asked, whose test fails until the project has one, so nothing
    merges by accident), its golden settings file, and the regenerated nomad.code-workspace.
    Then fast-forward master. Re-running is safe. Returns {ok, merged, branch, note}.

    `ptype` (#1373) is written as `type:`, and its profile's gates go into the autopilot block:
    they are read here from the profile, not taken from the caller. `policies` are attached at
    the project layer (e.g. engine-unity)."""
    pid = (pid or "").strip()
    if not PROJECT_ID.match(pid):
        return {"ok": False, "error": f"'{pid}' is not a usable project id."}
    prof, policies, why = _type_and_policies(ptype, policies)
    if why:
        return {"ok": False, "error": why}
    path = os.path.join(ROOTS[0], pid)
    if not os.path.isfile(os.path.join(path, "Nomad.md")):
        return {"ok": False, "error": f"no scaffolded repo at {path}: run /new-project first"}
    rc, listed = _run(["git", "show", f"{HUB_BRANCH}:projects.yaml"], HUB)
    if rc:
        return {"ok": False, "error": f"can't read projects.yaml on {HUB_BRANCH}"}
    if re.search(rf"^\s*- (id: |\{{id: ){re.escape(pid)}\b", listed, re.M):
        return {"ok": True, "merged": True, "branch": "", "note": "already registered"}
    branch, wt = f"register/{pid}", os.path.join(os.path.dirname(HUB), "nomad-wt", f"register-{pid}")
    why = _worktree(HUB, wt, branch, HUB_BRANCH)
    if why:
        return {"ok": False, "error": why}
    reg = os.path.join(wt, "projects.yaml")
    text = open(reg, encoding="utf-8").read()
    if not re.search(rf"^\s*- (id: |\{{id: ){re.escape(pid)}\b", text, re.M):
        rel = os.path.relpath(path, os.path.dirname(HUB))
        with open(reg, "a", encoding="utf-8") as f:
            f.write(("" if text.endswith("\n") else "\n") + _registry_entry(
                pid, rel, local_only, autopilot, prof.id, policies, prof.gates))
    paths = ["projects.yaml", "nomad.code-workspace"]
    try:
        if autopilot:
            golden = os.path.join("autopilot", "testdata", "golden", f"{pid}-settings.json")
            k = ["-m", "pytest", "-q", "-p", "no:cacheprovider", "autopilot/test_permissions.py",
                 "-k", f"golden and {pid}"]
            _run(PY + k, wt, timeout=300)                 # a missing golden is written, and fails
            rc, out = _run(PY + k, wt, timeout=300)
            if rc or not os.path.isfile(os.path.join(wt, golden)):
                return {"ok": False, "error": f"golden file check failed: {out[-400:]}", "branch": branch}
            paths.append(golden)
        before = _folders(os.path.join(wt, "nomad.code-workspace"))
        rc, out = _run(["env", f"NOMAD_WORKSPACE_HOME={os.path.dirname(HUB)}", *PY,
                        "scripts/gen-workspace.py"], wt, timeout=120)
        if rc == 0 and _folders(os.path.join(wt, "nomad.code-workspace")) < before:
            rc, out = 1, f"it dropped folders ({before} before): {out}"
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "error": f"timed out: {e.cmd[-1]}", "branch": branch}
    if rc:
        return {"ok": False, "error": f"gen-workspace failed: {out[-300:]}", "branch": branch}
    why = _commit(wt, paths, f"Register {pid} (NOMAD + New project)")
    if why:
        return {"ok": False, "error": why, "branch": branch}
    why = _ff_into(HUB, branch, HUB_BRANCH)
    if why:
        return {"ok": True, "merged": False, "branch": branch,
                "note": f"Registered on {branch}; not merged into {HUB_BRANCH} yet: {why}"}
    _drop(HUB, wt, branch, HUB_BRANCH)
    return {"ok": True, "merged": True, "branch": branch, "note": ""}


# ── the project's Zulip stream (POST /project-stream, §6.4 step 3.5) ─────────────────────
RELAY = os.path.join(HUB, "nomad-relay")
RELAY_ENV = os.path.expanduser(os.environ.get("NOMAD_RELAY_ENV", "~/.config/nomad/relay.env"))


def project_stream(pid):
    """Create #<id> with the operator and the bot subscribed, through the relay's
    project_streams.py (idempotent). Returns {ok, stream, result}."""
    pid = (pid or "").strip()
    if not PROJECT_ID.match(pid):
        return {"ok": False, "error": f"'{pid}' is not a usable stream name."}
    env = dict(os.environ)
    try:
        for line in open(RELAY_ENV, encoding="utf-8"):
            k, sep, v = line.strip().partition("=")
            if sep and k.startswith("NOMAD_ZULIP_"):
                env[k] = v.strip().strip('"')
    except OSError:
        return {"ok": False, "error": f"can't read {RELAY_ENV}"}
    try:
        p = subprocess.run([os.path.join(RELAY, ".venv", "bin", "python"), "project_streams.py", pid],
                           cwd=RELAY, env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith(f"#{pid}:")), "")
    result = line.split(":", 1)[1].strip() if line else (p.stderr or p.stdout).strip()[-300:]
    return {"ok": p.returncode == 0 and result == "ok", "stream": pid, "result": result}


# ── delete a project from Open WebUI (#1200, docs/prd/owui-sessions.md §6.6) ─────────────
# The reverse of the routes above. Nothing here erases data: the repo moves to ~/archive, the board
# and the stream are archived. The v2 engine asks for the operator's approval first and calls these
# one by one; each is safe to run twice, so a retry after a failure resumes.
HUB_IDS = {"nomad", "nomad-pm"}                   # never deleted, whatever the caller says
PROTECTED_BOARDS = {"nomad-work", "nomad-portfolio", "nomad-pm"}
ARCHIVE = os.environ.get("NOMAD_ARCHIVE_DIR", os.path.join(ROOTS[0], "archive"))
OP_CONTAINER = os.environ.get("NOMAD_OP_CONTAINER", "nomad-openproject")
GOLDEN = os.path.join("autopilot", "testdata", "golden", "{}-settings.json")
_ENTRY = r"^  - (?:id: |\{{id: ){}(?![\w-])"


def _hub_flagged(pid, entry=None):
    return pid in HUB_IDS or bool(entry and entry.get("hub") is True)


def _top_level(path):
    return os.path.dirname(os.path.normpath(path)) == os.path.normpath(ROOTS[0])


def project_info(pid):
    """Everything a delete would touch, read only, for the operator's approval. Returns {ok,
    known, hub, path, name, board, remote, dirty, golden, refuse}; `refuse` is why this project
    can't be deleted from a chat at all, else ""."""
    pid = (pid or "").strip()
    if not PROJECT_ID.match(pid):
        return {"ok": False, "error": f"'{pid}' is not a usable project id."}
    listed = load_projects()
    if listed is None:
        return {"ok": False, "error": "can't read projects.yaml"}
    entry = next((e for e in listed if e["id"] == pid), None)
    if entry is None:
        return {"ok": True, "known": False, "refuse": f"`{pid}` is not in projects.yaml."}
    path = entry["path"]
    hub = _hub_flagged(pid, entry)
    board = _board_for()(pid)
    remotes = []
    dirty = 0
    if os.path.isdir(path):
        rc, out = _run(["git", "remote", "-v"], path)
        remotes = sorted({ln.split()[1] for ln in out.splitlines() if rc == 0 and len(ln.split()) > 1})
        dirty = len([ln for ln in _run(["git", "status", "--porcelain"], path)[1].splitlines() if ln.strip()])
    refuse = ""
    if hub:
        refuse = f"`{pid}` is a hub repo, and a chat never deletes a hub repo."
    elif os.path.islink(path) or not _top_level(path):
        refuse = f"`{pid}` is not a plain top-level repo ({path}), so it is not moved from a chat."
    elif board != pid:
        refuse = f"`{pid}` shares the board `{board}` with other projects, so it is not deleted from a chat."
    elif pid in PROTECTED_BOARDS:
        refuse = f"`{pid}` is a protected board."
    rc, ls = _run(["git", "ls-tree", "--name-only", HUB_BRANCH, GOLDEN.format(pid)], HUB)
    return {"ok": True, "known": True, "hub": hub, "path": path, "name": _frontmatter(path).get("name") or pid,
            "board": board, "remote": remotes, "dirty": dirty, "golden": bool(rc == 0 and ls.strip()),
            "archive": os.path.join(ARCHIVE, pid), "refuse": refuse}


def archive_project(pid, confirm_remote=False):
    """Move the repo to ~/archive/<id> (never rm). Re-running after the move says so. Refuses a
    hub repo, a symlink, a grouped repo, and a repo with a git remote unless `confirm_remote`:
    the remote itself is never touched. Returns {ok, path, existed, note}."""
    info = project_info(pid)
    dest = os.path.join(ARCHIVE, (pid or "").strip())
    if not info.get("ok"):
        return info
    if not info["known"]:
        if os.path.isdir(dest):
            return {"ok": True, "path": dest, "existed": True, "note": "already archived"}
        return {"ok": False, "error": info["refuse"]}
    if info["refuse"]:
        return {"ok": False, "error": info["refuse"]}
    src = info["path"]
    if not os.path.isdir(src):
        if os.path.isdir(dest):
            return {"ok": True, "path": dest, "existed": True, "note": "already archived"}
        return {"ok": False, "error": f"no repo at {src} and none in {ARCHIVE}"}
    if info["remote"] and confirm_remote is not True:
        return {"ok": False, "error": f"it has a git remote ({', '.join(info['remote'])}); "
                                      "the operator has not confirmed archiving it"}
    if os.path.exists(dest):
        return {"ok": False, "error": f"{dest} already exists; not overwriting it"}
    os.makedirs(ARCHIVE, exist_ok=True)
    try:
        shutil.move(src, dest)
    except OSError as e:
        return {"ok": False, "error": f"move failed: {e}"}
    wt = src + "-wt"
    return {"ok": True, "path": dest, "existed": False,
            "note": f"worktrees in {wt} were not moved" if os.path.isdir(wt) else ""}


def _drop_entry(text, pid):
    """projects.yaml without the entry for `pid` (a block or a one-line flow mapping), keeping
    every comment and every other entry as written. Unchanged text when it isn't listed."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(_ENTRY.format(re.escape(pid)), ln)), None)
    if start is None:
        return text
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) >= 4):
        if not lines[end].strip():
            break
        end += 1
    return "".join(lines[:start] + lines[end:])


def unregister_hub(pid):
    """Remove a project from ~/nomad/projects.yaml on an `unregister/<id>` branch: the entry, its
    golden settings file, and the regenerated nomad.code-workspace. Then fast-forward master, as
    register_hub does. Re-running is safe. Returns {ok, merged, branch, note}."""
    pid = (pid or "").strip()
    if not PROJECT_ID.match(pid):
        return {"ok": False, "error": f"'{pid}' is not a usable project id."}
    rc, listed = _run(["git", "show", f"{HUB_BRANCH}:projects.yaml"], HUB)
    if rc:
        return {"ok": False, "error": f"can't read projects.yaml on {HUB_BRANCH}"}
    import yaml
    try:
        entries = (yaml.safe_load(listed) or {}).get("projects") or []
    except yaml.YAMLError as e:
        return {"ok": False, "error": f"projects.yaml on {HUB_BRANCH} doesn't parse: {e}"}
    entry = next((e for e in entries if isinstance(e, dict) and str(e.get("id")) == pid), None)
    if _hub_flagged(pid, entry):
        return {"ok": False, "error": f"`{pid}` is a hub repo."}
    branch, wt = f"unregister/{pid}", os.path.join(os.path.dirname(HUB), "nomad-wt", f"unregister-{pid}")
    pending = _run(["git", "rev-parse", "--verify", "-q", branch], HUB)[0] == 0
    if entry is None and not pending:
        return {"ok": True, "merged": True, "branch": "", "note": "already unregistered"}
    why = _worktree(HUB, wt, branch, HUB_BRANCH)
    if why:
        return {"ok": False, "error": why}
    reg = os.path.join(wt, "projects.yaml")
    text = open(reg, encoding="utf-8").read()
    if re.search(_ENTRY.format(re.escape(pid)), text, re.M):
        with open(reg, "w", encoding="utf-8") as f:
            f.write(_drop_entry(text, pid))
    paths = ["projects.yaml", "nomad.code-workspace"]
    golden = GOLDEN.format(pid)
    if os.path.isfile(os.path.join(wt, golden)):
        os.remove(os.path.join(wt, golden))
        paths.append(golden)
    try:
        before = _folders(os.path.join(wt, "nomad.code-workspace"))
        rc, out = _run(["env", f"NOMAD_WORKSPACE_HOME={os.path.dirname(HUB)}", *PY,
                        "scripts/gen-workspace.py"], wt, timeout=120)
        if rc == 0 and _folders(os.path.join(wt, "nomad.code-workspace")) < before - 1:
            rc, out = 1, f"it dropped more than one folder ({before} before): {out}"
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "error": f"timed out: {e.cmd[-1]}", "branch": branch}
    if rc:
        return {"ok": False, "error": f"gen-workspace failed: {out[-300:]}", "branch": branch}
    why = _commit(wt, paths, f"Unregister {pid} (NOMAD delete project, #1200)")
    if why:
        return {"ok": False, "error": why, "branch": branch}
    why = _ff_into(HUB, branch, HUB_BRANCH)
    if why:
        return {"ok": True, "merged": False, "branch": branch, "golden": golden in paths,
                "note": f"Unregistered on {branch}; not merged into {HUB_BRANCH} yet: {why}"}
    _drop(HUB, wt, branch, HUB_BRANCH)
    return {"ok": True, "merged": True, "branch": branch, "golden": golden in paths, "note": ""}


def _drop_board_row(seed_rb, ident):
    """seed.rb without the BOARDS row for `ident`, formatting of the other rows kept."""
    m, idents = _boards(seed_rb)
    if not m or ident not in idents:
        return seed_rb
    row = rf'\["{re.escape(ident)}",\s*"[^"]*"\]'
    body = m.group(1)
    new = re.sub(rf",\s*{row}(?=\s*\Z)", "", body, count=1)           # the last row: drop the comma before
    if new == body:
        new = re.sub(rf"{row},[ \t]*", "", body, count=1)
    new = "".join(ln for ln in new.splitlines(keepends=True) if ln.strip())
    return seed_rb[:m.start(1)] + new + seed_rb[m.end(1):]


def archive_board(ident):
    """Take the board's row out of ~/nomad-pm/seed.rb (on a branch, fast-forwarded into main like
    seed_board does), so no re-seed brings it back, then archive the OpenProject project from
    inside its container. The work packages stay, archived with it. Re-running is safe.
    Returns {ok, board, archived, merged, branch, note}."""
    ident = (ident or "").strip()
    if not PROJECT_ID.match(ident):
        return {"ok": False, "error": f"'{ident}' is not a usable board identifier."}
    if ident in PROTECTED_BOARDS or ident in HUB_IDS:
        return {"ok": False, "error": f"board `{ident}` is protected."}
    rc, main_rb = _run(["git", "show", f"{PM_BRANCH}:seed.rb"], PM_REPO)
    if rc:
        return {"ok": False, "error": f"can't read seed.rb on {PM_BRANCH} in {PM_REPO}"}
    res = {"ok": True, "board": ident, "merged": True, "branch": "", "note": ""}
    branch, wt = f"board-rm/{ident}", f"{PM_REPO}-wt/board-rm-{ident}"
    if ident in _boards(main_rb)[1] or _run(["git", "rev-parse", "--verify", "-q", branch], PM_REPO)[0] == 0:
        why = _worktree(PM_REPO, wt, branch, PM_BRANCH)
        if why:
            return {"ok": False, "error": why}
        seed = os.path.join(wt, "seed.rb")
        text = open(seed, encoding="utf-8").read()
        new = _drop_board_row(text, ident)
        if new != text:
            with open(seed, "w", encoding="utf-8") as f:
                f.write(new)
        why = _commit(wt, ["seed.rb"], f"Remove board {ident} (NOMAD delete project, #1200)")
        if why:
            return {"ok": False, "error": why}
        res["branch"] = branch
        why = _ff_into(PM_REPO, branch, PM_BRANCH)
        if why:
            res.update(merged=False, note=f"Branch {branch} isn't merged into {PM_BRANCH} yet: {why}")
        else:
            _drop(PM_REPO, wt, branch, PM_BRANCH)
    ruby = (f'pr = Project.find_by(identifier: "{ident}")\n'
            'if pr.nil?\n  puts "[archive] none"\nelse\n'
            '  if pr.active?\n'
            '    if defined?(Projects::ArchiveService)\n'
            '      Projects::ArchiveService.new(user: User.system, model: pr).call\n'
            '    else\n      pr.update_column(:active, false)\n    end\n  end\n'
            '  puts(pr.reload.active? ? "[archive] failed" : "[archive] done")\nend\n')
    tmp = f"/tmp/nomad-archive-{ident}.rb"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(ruby)
        rc, out = _run(["docker", "cp", tmp, f"{OP_CONTAINER}:{tmp}"], HUB, timeout=60)
        if rc:
            return {"ok": False, "error": f"docker cp failed: {out[-200:]}", **{k: res[k] for k in ("branch", "merged")}}
        rc, out = _run(["docker", "exec", OP_CONTAINER, "bash", "-c",
                        f"cd /app && bundle exec rails runner -e production {tmp}"], HUB, timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "archiving the board timed out", "branch": res["branch"]}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    mark = re.search(r"\[archive\] (done|none|failed)", out)
    if rc or not mark or mark.group(1) == "failed":
        return {"ok": False, "error": f"archive failed (rc {rc}): {out[-300:]}", "branch": res["branch"]}
    return {**res, "archived": mark.group(1) == "done"}


def archive_stream(pid):
    """Archive the project's Zulip stream through the relay's project_streams.py --archive,
    with the relay's credentials. A stream that isn't there counts as done. Returns {ok, stream, result}."""
    pid = (pid or "").strip()
    if not PROJECT_ID.match(pid) or pid in HUB_IDS:
        return {"ok": False, "error": f"'{pid}' is not a stream a chat may archive."}
    env = dict(os.environ)
    try:
        for line in open(RELAY_ENV, encoding="utf-8"):
            k, sep, v = line.strip().partition("=")
            if sep and k.startswith("NOMAD_ZULIP_"):
                env[k] = v.strip().strip('"')
    except OSError:
        return {"ok": False, "error": f"can't read {RELAY_ENV}"}
    try:
        p = subprocess.run([os.path.join(RELAY, ".venv", "bin", "python"), "project_streams.py",
                            "--archive", pid], cwd=RELAY, env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith(f"#{pid}:")), "")
    result = line.split(":", 1)[1].strip() if line else (p.stderr or p.stdout).strip()[-300:]
    return {"ok": p.returncode == 0 and result.startswith("ok"), "stream": pid, "result": result}


# ── Save a generated image into a project's folder ──────────────────────────────────
# The v2 engine generates the image (ComfyUI/OpenAI/Firefly) then hands the bytes here; the
# dispatcher (native, with host FS write access) resolves which project the prompt named and saves
# the file inside it. The engine container never needs write access to the project tree.
DEFAULT_IMAGE_SUBDIR = os.environ.get("NOMAD_IMAGE_SUBDIR", "assets/nomad-images")


def session_project(req):
    """POST /session/project: the registry project a mention on a board runs in (sessions.py)."""
    hub = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if hub not in sys.path:
        sys.path.insert(0, hub)
    from autopilot import tasks   # the BOARDS map; the hub is this file's parent
    listed = load_projects()
    if listed is None:            # no readable registry: the scan's slugs are the ids
        entries = [(r["slug"], r["path"]) for r in registry()]
    else:
        entries = [(e["id"], e["path"]) for e in listed
                   if e["dispatch"] and os.path.isdir(e["path"])]
    out = sessions.project_for(str(req.get("board") or ""), req.get("comment", ""),
                               req.get("subject", ""), entries, tasks.board_for)
    if out.get("project") and req.get("key"):
        path = dict(entries)[out["project"]]
        out["session"] = sessions.preview(str(req["key"]), path, req.get("subject", ""))
    return out


def resolve_from_text(text):
    """Find which known project is NAMED in free text. Matches ONLY the full project name or its
    directory slug (specific identifiers) — NOT the short prefix aliases used for `resolve()`, since
    those are generic words ('agent', 'crypto', 'content') that false-match prose. Longest match wins
    (so 'crypto trading bot' beats 'crypto-trading-bot' ties cleanly). Two different repos matching at
    the same length is a real ambiguity, and saving into the wrong one is silent, so that returns None.
    Returns the registry row or None."""
    t = (text or "").lower()
    best, tied = None, False
    for r in registry():
        for cand in {r["name"].lower(), r["slug"].lower(), *r["also"]}:
            if len(cand) >= 3 and re.search(r"\b" + re.escape(cand) + r"\b", t):
                if best is None or len(cand) > best[1]:
                    best, tied = (r, len(cand)), False
                elif len(cand) == best[1] and r["path"] != best[0]["path"]:
                    tied = True
    return None if (best is None or tied) else best[0]


def _parse_subdir(text):
    """Pull an explicit destination folder out of the prompt, else None (caller defaults)."""
    t = text or ""
    for p in (r"\b(?:sub)?folder[:\s]+([A-Za-z0-9._\-/]+)",
              r"\bdirectory[:\s]+([A-Za-z0-9._\-/]+)",
              r"\b(?:in|into|under|to)\s+(?:the\s+)?([A-Za-z0-9._\-/]{2,})\s+(?:folder|directory|dir)\b",
              r"[A-Za-z0-9]+/([A-Za-z0-9._\-/]+)"):   # explicit "<proj>/<sub/path>"
        m = re.search(p, t, re.I)
        if m:
            return m.group(1)
    return None


def _safe_dest(base, subdir, filename):
    """Resolve <base>/<subdir>/<filename>, refusing traversal outside base. Returns (dir, fname)."""
    sub = (subdir or DEFAULT_IMAGE_SUBDIR).strip().lstrip("/")
    target_dir = os.path.realpath(os.path.join(base, sub))
    base_real = os.path.realpath(base)
    if target_dir != base_real and not target_dir.startswith(base_real + os.sep):
        return None
    fn = os.path.basename((filename or "").strip()) or "nomad-image.png"
    if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        fn += ".png"
    return target_dir, fn


def save_image(text, filename, content_b64, subdir=None):
    """Resolve the project named in `text` and save the base64 image into it. Returns
    {ok, project, path, rel} or {ok:False, ...}. {no_project:True} = no project was named
    (so the caller can skip silently rather than treat it as a failure)."""
    proj = resolve_from_text(text)
    if not proj:
        return {"ok": False, "no_project": True,
                "error": "no known project named in the request",
                "known": [r["name"] for r in registry()]}
    dest = _safe_dest(proj["path"], subdir or _parse_subdir(text), filename)
    if not dest:
        return {"ok": False, "error": "unsafe destination folder (path traversal refused)"}
    target_dir, fn = dest
    try:
        data = base64.b64decode(content_b64 or "")
    except Exception as e:
        return {"ok": False, "error": f"bad base64: {str(e)[:80]}"}
    if not data:
        return {"ok": False, "error": "empty image payload"}
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, fn)
    with open(path, "wb") as f:
        f.write(data)
    return {"ok": True, "project": proj["name"], "path": path,
            "rel": os.path.relpath(path, proj["path"])}


# ── a whole project (POST /create-project, #1374) ──────────────────────────────────────
# One creation process: the engine runs nomad_projects.create, the module the Open WebUI chat
# runs, with the same setup (which calls back into /new-project, /register-project, /seed-board
# and /project-stream here; the server is threaded, so that can't deadlock). This only forwards.
ENGINE_URL = os.environ.get("NOMAD_ENGINE_URL", "http://127.0.0.1:8099").rstrip("/")
OWUI_TOKEN_FILE = os.path.expanduser(os.environ.get("NOMAD_OWUI_TOKEN_FILE",
                                                    "~/.config/nomad/owui-token"))


def _owui_token():
    tok = os.environ.get("NOMAD_OWUI_TOKEN", "").strip()
    if not tok and os.path.isfile(OWUI_TOKEN_FILE):
        with open(OWUI_TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    return tok


def _engine(method, path, body=None, timeout=60):
    """The engine's JSON answer, a refusal included; {ok: false, error} when it can't be asked."""
    import urllib.error
    import urllib.request
    tok = _owui_token()
    if not tok:
        return {"ok": False, "error": f"no Open WebUI token (NOMAD_OWUI_TOKEN or {OWUI_TOKEN_FILE})"}
    req = urllib.request.Request(ENGINE_URL + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Nomad-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}") or {"ok": False, "error": f"engine HTTP {e.code}"}
        except ValueError:
            return {"ok": False, "error": f"engine HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"the engine can't be reached: {type(e).__name__}"}


def create_project(req):
    return _engine("POST", "/owui/create-project", req if isinstance(req, dict) else {})


def create_status(pid):
    pid = (pid or "").strip()
    if not PROJECT_ID.match(pid):
        return {"ok": False, "error": f"'{pid}' is not a usable project id."}
    return _engine("GET", f"/owui/create-project/{pid}", timeout=15)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        """True if the caller presented the shared secret. /health is exempt so a liveness
        probe never needs the token; everything else can edit code, so nothing else is."""
        if hmac.compare_digest(self.headers.get("X-Nomad-Token", ""), TOKEN):
            return True
        self._send(401, {"ok": False, "error": "missing or wrong X-Nomad-Token"})
        return False

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            found = claude_found()
            self._send(200 if found else 503, {"status": "ok" if found else "claude missing",
                                               "auth": "token", "claude": CLAUDE, "claude_found": found})
        elif not self._authed():
            return
        elif self.path.rstrip("/") == "/projects":
            self._send(200, projects_payload())
        elif self.path.rstrip("/") == "/profiles":
            self._send(200, profiles_payload())
        elif urllib.parse.urlsplit(self.path).path.rstrip("/") in POLICY_GETS:
            u = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            self._send(*policies_route(u.path.rstrip("/"), q))
        elif urllib.parse.urlsplit(self.path).path.rstrip("/") in PROFILE_GETS:
            u = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if u.path.rstrip("/") == "/profiles/dry-run":
                q = {"id": q.get("id", "")}          # a GET dry-runs master's version only
            self._send(*profiles_route(u.path.rstrip("/"), q))
        elif urllib.parse.urlsplit(self.path).path.rstrip("/") == "/create-project":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._send(200, create_status((q.get("id") or [""])[0]))
        elif urllib.parse.urlsplit(self.path).path.rstrip("/") == "/project-context":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            ctx = project_context((q.get("id") or [""])[0])
            if ctx is None:
                self._send(404, {"ok": False, "error": "no such dispatchable project"})
            else:
                self._send(200, ctx)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        route = self.path.rstrip("/")
        if not self._authed():
            return
        if route in PROFILE_POSTS:
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                self._send(400, {"ok": False, "error": "body must be JSON"})
                return
            if not isinstance(req, dict):
                self._send(400, {"ok": False, "error": "body must be a JSON object"})
                return
            self._send(*profiles_route(route, req))
            return
        if route in POLICY_POSTS:
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                self._send(400, {"ok": False, "error": "body must be JSON"})
                return
            if not isinstance(req, dict):
                self._send(400, {"ok": False, "error": "body must be a JSON object"})
                return
            given = self.headers.get("X-Nomad-Cockpit", "")
            cockpit = bool(COCKPIT_TOKEN) and bool(given) and hmac.compare_digest(given, COCKPIT_TOKEN)
            self._send(*policies_route(route, req, cockpit))
            return
        if route not in ("/dispatch", "/create-project", "/new-project", "/seed-board", "/register-project",
                         "/project-stream", "/project-info", "/archive-project",
                         "/unregister-project", "/archive-board", "/archive-stream", "/verify", "/save-image", "/resolve", "/session",
                         "/session/close", "/session/project", "/comfy/generate"):
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            if route == "/create-project":
                self._send(200, create_project(req))
                return
            if route == "/new-project":
                self._send(200, new_project(
                    req.get("name", ""), req.get("description", ""), id=req.get("id", ""),
                    prd=req.get("prd", ""), stack=req.get("stack") or (),
                    lane=req.get("lane", "dev"), local_only=req.get("local_only") is True,
                    register=req.get("register", True) is not False,
                    template=req.get("template", "")))
                return
            if route == "/seed-board":
                self._send(200, seed_board(req.get("board", ""), req.get("name", "")))
                return
            if route == "/register-project":
                self._send(200, register_hub(req.get("id", ""), req.get("local_only") is True,
                                             req.get("autopilot") is True, req.get("type") or "",
                                             req.get("policies") or ()))
                return
            if route == "/project-stream":
                self._send(200, project_stream(req.get("id", "")))
                return
            if route == "/project-info":
                self._send(200, project_info(req.get("id", "")))
                return
            if route == "/archive-project":
                self._send(200, archive_project(req.get("id", ""), req.get("confirm_remote") is True))
                return
            if route == "/unregister-project":
                self._send(200, unregister_hub(req.get("id", "")))
                return
            if route == "/archive-board":
                self._send(200, archive_board(req.get("board", "")))
                return
            if route == "/archive-stream":
                self._send(200, archive_stream(req.get("id", "")))
                return
            if route == "/resolve":
                self._send(200, check_project(req.get("project", "")))
                return
            if route == "/verify":
                self._send(200, verify(req.get("project", ""), req.get("command", "")))
                return
            if route == "/comfy/generate":
                self._send(200, comfy_gate.generate(req.get("graph"), req.get("timeout")))
                return
            if route == "/save-image":
                self._send(200, save_image(req.get("text", ""), req.get("filename", ""),
                                           req.get("content_b64", ""), req.get("subdir")))
                return
            if route == "/session/project":
                self._send(200, session_project(req))
                return
            if route == "/session/close":
                self._send(200, sessions.close(req))
                return
            if route == "/session":
                proj = resolve(req.get("project", ""))
                if not proj or proj.get("ambiguous"):
                    self._send(200, _unresolved(req.get("project", ""), proj))
                    return
                self._send(200, sessions.run(req, lambda _p: proj, default_branch, CLAUDE))
                return
            mode = "plan" if req.get("mode") == "plan" else "build"
            proj = resolve(req.get("project", ""))
            if not proj or proj.get("ambiguous"):
                self._send(200, _unresolved(req.get("project", ""), proj))
                return
            task = (req.get("task") or "").strip()
            if not task:
                self._send(200, {"ok": False, "error": "Empty task."})
                return
            branch = ""
            if mode == "build":
                branch, err = ensure_feature_branch(proj["path"], task, req.get("branch"))
                if err:
                    self._send(200, {"ok": False, "error": err})
                    return
            t0 = time.time()
            summary, cost = run_claude(proj["path"], task, mode, branch,
                                       str(req.get("plan") or ""), proj.get("subdir", ""))
            resp = {"ok": True, "project": proj["name"], "path": proj["path"],
                    "mode": mode, "summary": summary, "secs": int(time.time() - t0),
                    "cost_usd": cost}
            if mode == "build":
                resp["branch"] = branch
                resp["diffstat"] = git(proj["path"], "diff", "--stat")
                resp["changed"] = [l for l in git(proj["path"], "status", "--porcelain").splitlines()]
            self._send(200, resp)
        except subprocess.TimeoutExpired:
            self._send(200, {"ok": False, "error": f"Builder timed out after {TIMEOUT}s."})
        except Exception as e:
            self._send(200, {"ok": False, "error": str(e)[:400]})


if __name__ == "__main__":
    print(f"NOMAD dispatcher on http://{HOST}:{PORT} (claude={CLAUDE}, roots={ROOTS}, "
          f"token={TOKEN_PATH}, {len(registry())} projects)", flush=True)
    bridge_bind.serve_http(HOST, PORT, Handler)   # + the Docker bridge gateway (#383)
