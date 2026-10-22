#!/usr/bin/env python3
"""Read-only checks for agent docs: size budget, stale memory, drift, worktree hygiene.

Usage:
    python3 checks.py [all|size|memory|drift|worktrees] [--repo DIR ...] [--from-projects]
                      [--config-dir DIR] [--wt-glob GLOB] [--threshold BYTES]

With no --repo and no --from-projects it checks the git repo around the current directory.
--from-projects reads the repo list from `git -C ~/nomad show master:projects.yaml` (master, not
the working copy, because other sessions switch that checkout).

Reports only. It never writes a file, changes a repository or removes a worktree, it makes no
network request, and it prints paths, sizes and ref names, never a file's contents. Commands it
proposes are quoted with shlex.quote and are for a person to read before running. Always exits 0.

Adapted from Upkeep's checks.py (Agulhas Labs, Apache-2.0) for NOMAD's layout: several repos with
worktrees under ~/<repo>-wt/, per-project memory under ~/.claude/projects/*/memory/, and skills
installed as copies with a SOURCE.md naming the repo, path and commit they came from.
"""
import argparse
import glob
import os
import re
import shlex
import subprocess
import sys
from typing import Optional, TypeGuard

DOC_BUDGET = 4096                  # CLAUDE.md, AGENTS.md, Nomad.md: every byte loads every session
MEMORY_INDEX_LINES = 200           # MEMORY.md is loaded into context; past this it gets truncated
MEMORY_INDEX_BYTES = 25 * 1024
DOC_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "Nomad.md")
STAMP = re.compile(r"managed by[:\s]+`?([^\s`>*]+)", re.IGNORECASE)
HOME = os.path.expanduser("~")


def config_dir(override=None):
    return override or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def git(root, *args) -> Optional[subprocess.CompletedProcess]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                              env=env, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def ok(done: Optional[subprocess.CompletedProcess]) -> TypeGuard[subprocess.CompletedProcess]:
    return bool(done and done.returncode == 0)


# ── repos ────────────────────────────────────────────────────────────────────────────────────

PROJECT_PATH = re.compile(r"(?:^\s*-?\s*|[{,]\s*)path:\s*([^\s,}#]+)")


def repos_from_projects(hub=os.path.join(HOME, "nomad")):
    """Repo dirs named in projects.yaml on the hub's master. Stdlib only, so a regex, not YAML."""
    done = git(hub, "show", "master:projects.yaml")
    if not ok(done):
        return []
    found = []
    for line in done.stdout.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in PROJECT_PATH.finditer(line.split(" #")[0]):
            path = os.path.join(HOME, m.group(1).strip("'\""))
            if path not in found and os.path.exists(os.path.join(path, ".git")):
                found.append(path)
    return found


def toplevel(path):
    done = git(path, "rev-parse", "--show-toplevel")
    return done.stdout.strip() if ok(done) else None


def default_branch(root):
    for name in ("master", "main"):
        if ok(git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")):
            return name
    return None


# ── (a) size budget ──────────────────────────────────────────────────────────────────────────

def docs_in(root):
    out = []
    for base in (root, os.path.join(root, ".claude")):
        out += [os.path.join(base, n) for n in DOC_NAMES if os.path.isfile(os.path.join(base, n))]
    return out


def size_budget(repos, cfg, threshold=DOC_BUDGET):
    rows = []
    paths = [os.path.join(cfg, "CLAUDE.md")] + [p for r in repos for p in docs_in(r)]
    for path in paths:
        if not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        if size > threshold:
            rows.append(f"{size / 1024:.1f} KB (~{size // 4} tokens, budget {threshold / 1024:.0f} KB)  {path}\n"
                        "    propose: move history to docs/, link SDLC.md and SHARED-RESOURCES.md instead "
                        "of restating them, keep only what is true now")
    for index in sorted(glob.glob(os.path.join(cfg, "projects", "*", "memory", "MEMORY.md"))):
        text = read(index) or ""
        lines, size = len(text.splitlines()), len(text.encode("utf-8"))
        if lines > MEMORY_INDEX_LINES or size > MEMORY_INDEX_BYTES:
            rows.append(f"{lines} lines, {size / 1024:.1f} KB  {index}\n"
                        f"    propose: one line per memory, under {MEMORY_INDEX_LINES} lines and "
                        f"{MEMORY_INDEX_BYTES // 1024} KB; merge or drop stale entries")
    return rows or ["size: every doc is within budget"]


# ── (b) stale memory ─────────────────────────────────────────────────────────────────────────

ABS_PATH = re.compile(r"(?<![\w/.-])((?:~|/home/[\w.-]+)/[\w./+@-]*[\w/])")
INDEX_LINK = re.compile(r"\]\(([^)\s]+\.md)\)")


def stale_memory(cfg):
    rows = []
    for mem in sorted(glob.glob(os.path.join(cfg, "projects", "*", "memory"))):
        files = sorted(n for n in os.listdir(mem) if n.endswith(".md") and n != "MEMORY.md")
        index = read(os.path.join(mem, "MEMORY.md"))
        if index is not None:
            linked = INDEX_LINK.findall(index)
            for name in dict.fromkeys(linked):
                if not os.path.isfile(os.path.join(mem, name)):
                    rows.append(f"{mem}/MEMORY.md\n    index points at a missing file: {name}")
            unlisted = [n for n in files if n not in linked]
            if unlisted:
                rows.append(f"{mem}/MEMORY.md\n    memory files not in the index: " + ", ".join(unlisted))
        elif files:
            rows.append(f"{mem}\n    {len(files)} memory file(s) but no MEMORY.md index")
        for name in files:
            text = read(os.path.join(mem, name)) or ""
            gone = [p for p in dict.fromkeys(ABS_PATH.findall(text))
                    if "*" not in p and "<" not in p and not os.path.exists(os.path.expanduser(p))]
            if gone:
                rows.append(f"{mem}/{name}\n    names paths that no longer exist: " + ", ".join(gone))
    if rows:
        rows.append("(memory hits are candidates: a path may be gone on purpose, or live on another "
                    "machine. Check before editing a memory.)")
    return rows or ["memory: every index entry resolves and no memory names a missing path"]


# ── (c) drift ────────────────────────────────────────────────────────────────────────────────

SOURCE_FIELD = re.compile(r"^(repo|path|commit):\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


def drift(repos, cfg):
    """Two kinds of installed copy can drift from their source:
    1. a doc whose first lines say "managed by <path>" (Upkeep's stamp), compared with that file;
    2. a skill under <cfg>/skills/ whose SOURCE.md names a local `repo:` and `path:`, compared
       file by file with that path on the repo's default branch. Remote sources (a URL) are skipped:
       checking them would need the network."""
    rows = []
    stamped = [os.path.join(cfg, "CLAUDE.md")] + [p for r in repos for p in docs_in(r)]
    for path in stamped:
        text = read(path)
        if text is None:
            continue
        m = STAMP.search("\n".join(text.splitlines()[:5]))
        if not m:
            continue
        src = os.path.expanduser(m.group(1))
        src = src if os.path.isabs(src) else os.path.join(os.path.dirname(path), src)
        origin = read(src)
        body = lambda t: [l for l in t.splitlines() if not STAMP.search(l)]
        if origin is None:
            rows.append(f"{path}\n    the source named in its stamp is missing: {src}")
        elif body(text) != body(origin):
            rows.append(f"{path}\n    differs from its source {src}")
    for source_md in sorted(glob.glob(os.path.join(cfg, "skills", "*", "SOURCE.md"))):
        fields = {k.lower(): v for k, v in SOURCE_FIELD.findall(read(source_md) or "")}
        repo, rel = os.path.expanduser(fields.get("repo", "")), fields.get("path", "")
        if not repo or not rel or not os.path.isdir(repo):
            continue
        base = default_branch(repo)
        listed = git(repo, "ls-tree", "-r", "--name-only", f"{base}:{rel}") if base else None
        if not ok(listed):
            rows.append(f"{source_md}\n    {rel} is not on {base} in {repo}")
            continue
        skill = os.path.dirname(source_md)
        changed = []
        for name in listed.stdout.split():
            if name.startswith("tests/"):
                continue
            want = git(repo, "show", f"{base}:{rel}/{name}")
            if read(os.path.join(skill, name)) != (want.stdout if ok(want) else None):
                changed.append(name)
        if changed:
            rows.append(f"{skill}\n    differs from {repo} {base}:{rel}: " + ", ".join(changed) +
                        f"\n    propose: reinstall from {base} and update SOURCE.md's commit")
    return rows or ["drift: no installed copy differs from its source"]


# ── (d) worktree hygiene ─────────────────────────────────────────────────────────────────────

def worktrees(root):
    done = git(root, "worktree", "list", "--porcelain")
    entries, cur = [], {}
    for line in (done.stdout if ok(done) else "").splitlines() + [""]:
        if not line:
            if cur:
                entries.append(cur)
            cur = {}
        else:
            key, _, value = line.partition(" ")
            cur[key] = value
    return entries


def merged(root, branch, base):
    """An ancestor of base with commits of its own. A branch still at base's tip is usually work
    that has not started, so it does not count."""
    tip = git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    base_tip = git(root, "rev-parse", "--verify", "--quiet", base)
    if ok(tip) and ok(base_tip) and tip.stdout == base_tip.stdout:
        return False
    return ok(git(root, "merge-base", "--is-ancestor", f"refs/heads/{branch}", base))


def worktree_hygiene(repos, wt_glob=os.path.join(HOME, "*-wt")):
    rows = []
    wt_roots = [os.path.realpath(p) for p in glob.glob(wt_glob)]
    under = lambda p: any(os.path.realpath(p).startswith(r + os.sep) for r in wt_roots)
    for root in repos:
        base = default_branch(root)
        for e in worktrees(root):
            where = e.get("worktree", "")
            if not under(where) or "locked" in e:
                continue
            branch = e.get("branch", "").removeprefix("refs/heads/")
            q = shlex.quote
            if "prunable" in e or not os.path.isdir(where):
                rows.append(f"worktree {where} (repo {root}) is registered but its directory is gone\n"
                            f"    propose: git -C {q(root)} worktree prune")
            elif base and branch and merged(root, branch, base):
                rows.append(f"worktree {where} (branch {branch}) is merged into {base}\n"
                            f"    propose: git -C {q(root)} worktree remove {q(where)} && "
                            f"git -C {q(root)} branch -d {q(branch)}")
    if rows:
        rows.append("(another session may still be using a worktree: check it is idle before removing. "
                    "Not detected: squash-merged branches, detached worktrees.)")
    return rows or ["worktrees: none under the *-wt dirs is merged or missing"]


# ── main ─────────────────────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("check", nargs="?", default="all", choices=("all", "size", "memory", "drift", "worktrees"))
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--from-projects", action="store_true")
    ap.add_argument("--config-dir")
    ap.add_argument("--wt-glob", default=os.path.join(HOME, "*-wt"))
    ap.add_argument("--threshold", type=int, default=DOC_BUDGET)
    a = ap.parse_args(argv)
    cfg = config_dir(a.config_dir)
    repos = [t for t in (toplevel(os.path.expanduser(r)) for r in a.repo) if t]
    if a.from_projects:
        repos += [r for r in repos_from_projects() if r not in repos]
    if not repos and not a.repo:
        here = toplevel(os.getcwd())
        repos = [here] if here else []
    checks = {
        "size": lambda: size_budget(repos, cfg, a.threshold),
        "memory": lambda: stale_memory(cfg),
        "drift": lambda: drift(repos, cfg),
        "worktrees": lambda: worktree_hygiene(repos, a.wt_glob),
    }
    for name in (checks if a.check == "all" else [a.check]):
        print(f"== {name}")
        print("\n".join(checks[name]()))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
