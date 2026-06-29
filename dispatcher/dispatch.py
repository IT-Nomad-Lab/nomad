#!/usr/bin/env python3
"""NOMAD Builder dispatcher.

Runs headless Claude Code IN a target project's repo so the "Builder agent" can
actually implement a task there. Claude Code auto-reads the repo's CLAUDE.md, so
it works with full project context.

Endpoints (native WSL service; the console reaches it at host.docker.internal:8090):
  POST /dispatch {project, task, mode}   mode = "plan" (read-only) | "build" (edits)
  GET  /projects                          discovered Nomad.md projects
  GET  /health

GUARDRAILS:
- "build" runs with --permission-mode acceptEdits: Claude may EDIT files (reversible,
  uncommitted) but is instructed NEVER to git commit/push or run destructive commands.
- "plan" runs read-only (--permission-mode plan): no file changes at all.
- Only operates on dirs that carry a Nomad.md marker. Uses the local Claude Code login
  (subscription), never the Anthropic API key.
Review the working-tree diff before committing/pushing — commit/push stay human-gated.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("NOMAD_DISPATCH_HOST", "0.0.0.0")
PORT = int(os.environ.get("NOMAD_DISPATCH_PORT", "8090"))
ROOTS = os.environ.get("PROJECT_ROOTS", os.path.expanduser("~")).split(":")
CLAUDE = shutil.which("claude") or "/home/linuxbrew/.linuxbrew/bin/claude"
TIMEOUT = int(os.environ.get("NOMAD_DISPATCH_TIMEOUT", "900"))

GUARD = (
    "You are NOMAD's Builder agent, working directly inside this repository. "
    "Read CLAUDE.md / README first for context and conventions, then implement the "
    "operator's request by editing files as needed, matching the existing style. "
    "STRICT RULES: never run `git commit`, `git push`, or any destructive/irreversible "
    "command; leave ALL changes uncommitted for human review. When finished, give a "
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


def registry():
    out = []
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for nm in sorted(os.listdir(root)):
            d = os.path.join(root, nm)
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "Nomad.md")):
                m = _frontmatter(d)
                name = m.get("name") or nm
                aliases = {name.lower(), nm.lower(), nm.split("-")[0].lower()}
                out.append({"name": name, "path": d,
                            "lane": m.get("lane", ""), "repo": m.get("repo", ""),
                            "status": (m.get("status") or "Active").capitalize(),
                            "aliases": {a for a in aliases if len(a) >= 3}})
    return out


def resolve(project):
    p = (project or "").strip().lower()
    for r in registry():
        if p == r["name"].lower() or p in r["aliases"] or any(a in p for a in r["aliases"]):
            return r
    return None


def run_claude(path, task, mode):
    perm = "plan" if mode == "plan" else "acceptEdits"
    guard = PLAN_GUARD if mode == "plan" else GUARD
    cmd = [CLAUDE, "-p", task, "--model", "opus",
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
    if not proj:
        return {"ok": False, "error": f"Unknown project '{project}'. Known: {[r['name'] for r in registry()]}"}
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
    README + git init. Lands under the first PROJECT_ROOTS dir so it's immediately
    discoverable by NOMAD. Returns {ok, name, slug, path} (ok=False if it exists)."""
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
                      "- Keep changes small and reversible; commit/push stay human-gated.\n\n"
                      "## Next steps\n- [ ] Define the goal and first milestone.\n"),
        "README.md": f"# {name}\n\n{desc}\n\n_Scaffolded by NOMAD on {today}._\n",
        ".gitignore": ".env\n.env.*\n__pycache__/\n*.pyc\nnode_modules/\n.DS_Store\n",
    }
    for fn, body in files.items():
        with open(os.path.join(path, fn), "w", encoding="utf-8") as f:
            f.write(body)
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "-c", "user.email=nomad@example.com", "-c", "user.name=NOMAD",
        "commit", "-q", "-m", f"Scaffold {name} (NOMAD)")
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
    (so 'crypto trading bot' beats 'crypto-trading-bot' ties cleanly). Returns the registry row or None."""
    t = (text or "").lower()
    best = None
    for r in registry():
        for cand in {r["name"].lower(), os.path.basename(r["path"]).lower()}:
            if len(cand) >= 3 and re.search(r"\b" + re.escape(cand) + r"\b", t):
                if best is None or len(cand) > best[1]:
                    best = (r, len(cand))
    return best[0] if best else None


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

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok"})
        elif self.path.rstrip("/") == "/projects":
            reg = registry()
            self._send(200, {"projects": [r["name"] for r in reg],
                             "detail": [{"name": r["name"], "lane": r["lane"],
                                         "repo": r["repo"], "status": r["status"]} for r in reg]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        route = self.path.rstrip("/")
        if route not in ("/dispatch", "/new-project", "/verify", "/save-image"):
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            if route == "/new-project":
                self._send(200, new_project(req.get("name", ""), req.get("description", "")))
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
            if not proj:
                self._send(200, {"ok": False, "error": f"Unknown project '{req.get('project')}'. "
                                 f"Known: {[r['name'] for r in registry()]}"})
                return
            task = (req.get("task") or "").strip()
            if not task:
                self._send(200, {"ok": False, "error": "Empty task."})
                return
            t0 = time.time()
            summary, cost = run_claude(proj["path"], task, mode)
            resp = {"ok": True, "project": proj["name"], "path": proj["path"],
                    "mode": mode, "summary": summary, "secs": int(time.time() - t0),
                    "cost_usd": cost}
            if mode == "build":
                resp["diffstat"] = git(proj["path"], "diff", "--stat")
                resp["changed"] = [l for l in git(proj["path"], "status", "--porcelain").splitlines()]
            self._send(200, resp)
        except subprocess.TimeoutExpired:
            self._send(200, {"ok": False, "error": f"Builder timed out after {TIMEOUT}s."})
        except Exception as e:
            self._send(200, {"ok": False, "error": str(e)[:400]})


if __name__ == "__main__":
    print(f"NOMAD dispatcher on http://{HOST}:{PORT} (claude={CLAUDE}, roots={ROOTS})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
