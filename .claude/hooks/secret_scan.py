#!/usr/bin/env python3
"""NOMAD secret-scan gate (roadmap T0.3).

This repo has leaked a Notion token and an Anthropic API key before, and Wave 0 puts
more agents to work writing code. So the gate is enforced by the harness, not by an
agent remembering to check.

ONE mechanism, two entry points (defense in depth, same code both times):
  - .githooks/pre-commit   -> every commit, from any tool, agent or human shell
  - .claude/hooks/guard.py -> PreToolUse, denies an agent's commit before it runs

Only STAGED content is scanned, so an ordinary clean commit costs about half a second.

FAIL-CLOSED. A commit is cheap to redo; a published credential is not. If the scanner
cannot run it blocks and says why. gitleaks is the engine; if the binary goes missing
we drop to the built-in pattern set below rather than waving the commit through.

Output is REDACTED by construction: findings are reported as file:line + rule + a
masked token. The matched value is never printed, logged or written to a report.

Exit codes: 0 clean, 1 secret found, 2 scanner could not run (treat as blocked).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# --- fallback engine -------------------------------------------------------
# Used only when gitleaks is unavailable. High signal on purpose: a guard that
# fires on ordinary code gets switched off within a day, and then it guards nothing.
FALLBACK_RULES = [
    ("anthropic-api-key",   r"sk-ant-(?:api|admin)[0-9]{2}-[A-Za-z0-9_\-]{20,}"),
    ("openai-api-key",      r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}"),
    ("notion-api-token",    r"\b(?:ntn_|secret_)[A-Za-z0-9]{35,}"),
    ("github-pat",          r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
    ("github-fine-grained", r"\bgithub_pat_[A-Za-z0-9_]{60,}"),
    ("google-api-key",      r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("slack-token",         r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    ("aws-access-key-id",   r"\bAKIA[0-9A-Z]{16}\b"),
    ("private-key",         r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("jwt",                 r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ("generic-assignment",  r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret"
                            r"|secret[_-]?key|password|passwd)\b\s*[:=]\s*[\"']?([A-Za-z0-9/+_\-]{24,})"),
]
# Obvious non-secrets. Keeps the generic rule usable instead of noisy.
PLACEHOLDER = re.compile(
    r"(?i)(x{4,}|your[_-]?|change[_-]?me|replace[_-]?me|example|sample|placeholder|dummy|"
    r"redacted|test[_-]?key|<[^>]+>|\$\{|\$[A-Z_]+|\.\.\.|fake|noop|none|null|todo)")

# Staging one of these is the exact failure this track exists to stop.
ENV_FILE = re.compile(r"(^|/)\.env(\.[A-Za-z0-9_.-]+)?$")
ENV_FILE_OK = re.compile(r"(^|/)\.env\.example$")


def mask(value):
    """Never return anything usable. Prefix shape and length only."""
    v = value or ""
    pre = v[:4] if len(v) > 12 else ""
    return "{}{}[{} chars]".format(pre, "*" * 6, len(v))


def repo_root(start=None):
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start or os.getcwd(),
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def find_gitleaks():
    for cand in (os.environ.get("GITLEAKS_BIN"), shutil.which("gitleaks"),
                 os.path.expanduser("~/bin/gitleaks"), "/usr/local/bin/gitleaks"):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def staged_paths(root):
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                         cwd=root, capture_output=True, text=True, timeout=30)
    return [p for p in out.stdout.splitlines() if p.strip()]


def scan_env_files(root):
    """An env file staged by any route, including a forced add that defeats .gitignore."""
    hits = []
    for path in staged_paths(root):
        if ENV_FILE.search(path) and not ENV_FILE_OK.search(path):
            hits.append({"rule": "env-file-staged", "file": path, "line": 0,
                         "masked": "(whole file)",
                         "note": "an environment file is staged; these hold live credentials"})
    return hits


def scan_gitleaks(root, binary):
    fd, report = tempfile.mkstemp(suffix=".json", prefix="nomad-secret-scan-")
    os.close(fd)
    try:
        cmd = [binary, "git", root, "--staged", "--redact=100", "--report-format", "json",
               "--report-path", report, "--exit-code", "0", "--log-level", "error"]
        run = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=180)
        if run.returncode != 0:
            raise RuntimeError((run.stderr or run.stdout or "gitleaks failed").strip()[:400])
        with open(report) as fh:
            raw = fh.read().strip() or "[]"
        hits = []
        for f in json.loads(raw):
            hits.append({"rule": f.get("RuleID", "?"), "file": f.get("File", "?"),
                         "line": f.get("StartLine", 0), "masked": "[redacted by gitleaks]",
                         "note": (f.get("Description") or "")[:110]})
        return hits
    finally:
        try:
            os.unlink(report)
        except OSError:
            pass


def scan_fallback(root):
    """Added lines of the staged diff only. No gitleaks, no third-party import."""
    out = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR"],
                         cwd=root, capture_output=True, text=True, timeout=120)
    hits, path, lineno = [], "?", 0
    for line in out.stdout.splitlines():
        if line.startswith("+++ b/"):
            path, lineno = line[6:], 0
            continue
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        for rule, pat in FALLBACK_RULES:
            m = re.search(pat, body)
            if not m:
                continue
            value = m.group(m.lastindex or 0)
            if PLACEHOLDER.search(value):
                continue
            hits.append({"rule": rule, "file": path, "line": lineno,
                         "masked": mask(value), "note": "matched built-in fallback pattern"})
            break
        lineno += 1
    return hits


def scan_staged(root):
    """Returns (findings, engine_name). Raises on scanner failure, so the caller blocks."""
    findings = scan_env_files(root)
    binary = find_gitleaks()
    if binary:
        return findings + scan_gitleaks(root, binary), "gitleaks ({})".format(binary)
    return findings + scan_fallback(root), "built-in fallback (gitleaks not installed)"


def main():
    root = repo_root()
    if not root:
        print("SECRET SCAN: not a git repository; refusing to guess.", file=sys.stderr)
        return 2
    try:
        findings, engine = scan_staged(root)
    except Exception as exc:                       # fail-closed, on purpose
        print("SECRET SCAN COULD NOT RUN -> commit blocked.", file=sys.stderr)
        print("  reason: {}".format(exc), file=sys.stderr)
        print("  repair the scanner, or bypass once with --no-verify and say so "
              "in the commit message.", file=sys.stderr)
        return 2
    if not findings:
        return 0
    out = sys.stderr
    print("", file=out)
    print("COMMIT BLOCKED: {} possible credential(s) in the staged changes."
          .format(len(findings)), file=out)
    print("engine: {}".format(engine), file=out)
    print("values are redacted below and were never printed or written anywhere.", file=out)
    print("", file=out)
    for f in findings:
        print("  {}:{}  [{}]  {}".format(f["file"], f["line"], f["rule"], f["masked"]), file=out)
        if f.get("note"):
            print("      {}".format(f["note"]), file=out)
    print("""
What to do:
  1. Take the value OUT of the file. Put it in the gitignored env file and read it
     back with os.environ.
  2. Unstage it:   git restore --staged <file>
  3. If the value is real, ROTATE IT NOW. It has been on disk; treat it as exposed.
  4. False positive? Add a narrow path rule to .gitleaks.toml with a stated reason,
     or put a gitleaks:allow comment on that exact line. Never widen it to a directory.
  5. Real emergency only: re-run the commit with --no-verify.
""", file=out)
    return 1


if __name__ == "__main__":
    sys.exit(main())
