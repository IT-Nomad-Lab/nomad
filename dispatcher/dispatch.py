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
  GET  /projects                          discovered Nomad.md projects
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
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import projects_registry  # noqa: E402  (shared with autopilot/projects.py)

HOST = os.environ.get("NOMAD_DISPATCH_HOST", "127.0.0.1")   # loopback only (#821); containers still reach it via host.docker.internal
PORT = int(os.environ.get("NOMAD_DISPATCH_PORT", "8090"))
ROOTS = os.environ.get("PROJECT_ROOTS", "/home/jdelimam").split(":")
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")  # 2.x native
#   installer path, as in claude-bridge. The old linuxbrew one is gone, and every build failed on it
#   (#861). /health reports whether this exists, so the next move shows up in nomad-watch.
TIMEOUT = int(os.environ.get("NOMAD_DISPATCH_TIMEOUT", "900"))


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
            out.append(_row(e["path"], e["id"], e["aliases"]))
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
    cmd = [CLAUDE, "-p", builder_prompt(task, plan, subdir), "--model", "opus",
           "--append-system-prompt", guard,
           "--permission-mode", perm, "--output-format", "json"]
    proc = subprocess.run(cmd, cwd=path, capture_output=True, text=True,
                          timeout=TIMEOUT, env={**os.environ, "ANTHROPIC_API_KEY": ""})
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


def new_project(name, description=""):
    """Scaffold a brand-new NOMAD project repo: dir + Nomad.md marker + CLAUDE.md +
    README + git init. Lands under the first PROJECT_ROOTS dir and is appended to
    nomad/projects.yaml, so it's immediately discoverable by NOMAD. Returns {ok, name, slug, path} (ok=False if it exists)."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "Project needs a name."}
    slug = _slug(name)
    root = ROOTS[0]
    path = os.path.join(root, slug)
    if os.path.exists(path):
        return {"ok": False, "error": f"'{slug}' already exists at {path}."}
    today = date.today().isoformat()
    desc = description.strip() or f"{name} — a NOMAD-managed project."
    os.makedirs(path, exist_ok=True)
    files = {
        "Nomad.md": (f"---\nname: {name}\nstatus: Planning\nnomad: true\n"
                     f"created: {today}\n---\n\n# {name}\n\n{desc}\n\n"
                     "This repo is tracked by NOMAD (mission control + Builder dispatch).\n"),
        "CLAUDE.md": (f"# {name} — project guide\n\n> NOMAD-managed. Created {today}.\n\n"
                      f"## What this is\n{desc}\n\n## Status\nPlanning — scope being defined.\n\n"
                      "## Conventions\n- Secrets only in `.env` (never commit).\n"
                      "- Keep changes small and reversible; commit/push stay human-gated.\n"
                      "- Each new feature gets its own branch; merge to the default branch only "
                      "when the feature is done and all tests pass.\n\n"
                      "## Next steps\n- [ ] Define the goal and first milestone.\n"),
        "README.md": f"# {name}\n\n{desc}\n\n_Scaffolded by NOMAD on {today}._\n",
        ".gitignore": ".env\n.env.*\n__pycache__/\n*.pyc\nnode_modules/\n.DS_Store\n",
    }
    for fn, body in files.items():
        with open(os.path.join(path, fn), "w", encoding="utf-8") as f:
            f.write(body)
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "-c", "user.email=nomad@itnomadlab.com", "-c", "user.name=NOMAD",
        "commit", "-q", "-m", f"Scaffold {name} (NOMAD)")
    register_project(slug, path)
    return {"ok": True, "name": name, "slug": slug, "path": path}


# ── Save a generated image into a project's folder ──────────────────────────────────
# The v2 engine generates the image (ComfyUI/OpenAI/Firefly) then hands the bytes here; the
# dispatcher (native, with host FS write access) resolves which project the prompt named and saves
# the file inside it. The engine container never needs write access to the project tree.
DEFAULT_IMAGE_SUBDIR = os.environ.get("NOMAD_IMAGE_SUBDIR", "assets/nomad-images")


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
            reg = registry()
            self._send(200, {"projects": [r["name"] for r in reg],
                             "ids": [r["slug"] for r in reg],
                             "detail": [{"name": r["name"], "id": r["slug"], "lane": r["lane"],
                                         "repo": r["repo"], "status": r["status"]} for r in reg]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        route = self.path.rstrip("/")
        if not self._authed():
            return
        if route not in ("/dispatch", "/new-project", "/verify", "/save-image", "/resolve"):
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            if route == "/new-project":
                self._send(200, new_project(req.get("name", ""), req.get("description", "")))
                return
            if route == "/resolve":
                self._send(200, check_project(req.get("project", "")))
                return
            if route == "/verify":
                self._send(200, verify(req.get("project", ""), req.get("command", "")))
                return
            if route == "/save-image":
                self._send(200, save_image(req.get("text", ""), req.get("filename", ""),
                                           req.get("content_b64", ""), req.get("subdir")))
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
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
