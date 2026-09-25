#!/usr/bin/env python3
"""NOMAD v2 build-team guardrail (PreToolUse hook).

Routes IRREVERSIBLE actions to the operator for approval and flags SECRET exposure, so the
human gate is enforced by the harness — not by an agent remembering to ask. Scoped to Bash
and to the file-writing tools in .claude/settings.json. Decisions:
  - commit-gate bypass    -> "deny" (blocked; no agent skips the secret scan)
  - staged secret         -> "deny" (blocked; the scan runs before the commit does)
  - write to the guard    -> "deny" (blocked; see PROTECTED_PATHS)
  - irreversible pattern  -> "ask"  (operator confirms)
  - secret-exfil pattern  -> "deny" (blocked; reword)
  - otherwise             -> defer to normal permissions (exit 0, no decision)

On the write branch: a guard that only watches Bash cannot protect itself. Most agents in
.claude/agents/ hold Write and Edit, and an Edit that quietly neuters secret_scan.py fires
no hook at all, after which every commit passes a scan that no longer scans. So the files
that make up the gate are not editable by any agent, through any tool. The operator edits
them in their own editor; that asymmetry is the whole point, and it applies to the agent
maintaining this file too.

Mostly fail-open by design (a buggy guard must not brick the session); the security-reviewer
agent hardens this and the risk register tracks it (R3). The ONE fail-closed path is the
commit secret scan added for roadmap T0.3: if the scanner cannot run, the commit is denied,
because a leaked credential is not undoable and a blocked commit is.
"""
import json
import os
import re
import subprocess
import sys

# Irreversible / outward-facing / destructive — pause for the operator.
IRREVERSIBLE = [
    r"\bgit\s+push\b", r"\bgit\s+merge\b", r"\bgit\s+rebase\b\s+.*\bmain\b",
    r"\bgit\s+reset\s+--hard\s+origin", r"\bgit\s+push\s+.*--force",
    r"\bgh\s+pr\s+merge\b", r"\bgh\s+release\s+create\b",
    r"\bnpm\s+publish\b", r"\b(twine|pip)\s+upload\b",
    r"\bdocker\s+push\b", r"\bdocker\s+compose\s+down\s+.*-v", r"\bdocker\s+volume\s+rm\b",
    r"\bterraform\s+(apply|destroy)\b", r"\bkubectl\s+(apply|delete)\b",
    r"\bcloudflared\s+tunnel\b", r"\btailscale\s+funnel\b", r"\bpublish\.sh\b",
    r"\bn8n\s+update:workflow\b.*--active", r"--active=true",
    r"\brm\s+-rf\s+(?!/tmp|\./tmp|/var/tmp)\S", r"\bDROP\s+TABLE\b", r"\bTRUNCATE\b",
    r"\bDELETE\s+FROM\b", r"\bsend[_-]?email\b",
]
# Secret exfiltration — block and ask for a reword.
SECRETS = [
    r"\b(cat|less|head|tail|bat)\b[^|;&]*\.env(\b|[^a-z])",
    r"\becho\b[^|;&]*\$\{?[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD)",
    r"\bgit\s+add\b[^|;&]*\.env(\b|[^a-z.])", r"\bprintenv\b.*(KEY|TOKEN|SECRET)",
    r"id_rsa\b", r"\.pem\b",
]


# Commit bypass. The pre-commit gate is the last thing standing between a credential and
# a published repo, so an agent does not get to skip it. A human can, in their own shell.
COMMIT_BYPASS = [
    r"\bgit\s+commit\b[^|;&\n]*(--no-verify|\s-n\b)",
    r"\bgit\s+(commit|merge|rebase)\b[^|;&\n]*--no-verify",
    # Only the WRITE forms below. Reading the setting is how you verify the gate is
    # on, and a guard that blocks its own verification gets switched off.
    r"\bgit\s+config\b[^|;&\n]*core\.hooksPath[ \t]+\S",
    r"\bgit\s+config\b[^|;&\n]*--unset[^|;&\n]*core\.hooksPath",
    r"\bgit\s+config\b[^|;&\n]*core\.hooksPath[^|;&\n]*--unset",
    # Disarming the gate itself.
    r"\bchmod\b[^|;&\n]*-x[^|;&\n]*\.githooks",
    r"\brm\b[^|;&\n]*\.githooks/pre-commit",
    r"\brm\b[^|;&\n]*secret_scan\.py",
    # Editing the gate is as good as deleting it, and the shell has plenty of ways.
    # The Write/Edit tools are covered by PROTECTED_PATHS below; these are the Bash ones.
    r"\b(sed|perl|ruby)\b[^|;&\n]*-i[^|;&\n]*(secret_scan|guard)\.py",
    r"\b(sed|perl|ruby)\b[^|;&\n]*-i[^|;&\n]*(\.gitleaks\.toml|\.githooks/)",
    r"\btee\b[^|;&\n]*(secret_scan|guard)\.py",
    r"\btee\b[^|;&\n]*(\.gitleaks\.toml|\.githooks/)",
    r">>?[ \t]*\S*(secret_scan|guard)\.py",
    r">>?[ \t]*\S*(\.gitleaks\.toml|\.githooks/)",
    r"\b(chmod|mv|truncate)\b[^|;&\n]*(secret_scan|guard)\.py",
    r"\brm\b[^|;&\n]*(guard\.py|\.gitleaks\.toml)",
]

# The gate's own files. Nothing an agent runs may write here, by any tool. Paths are
# relative to PROJECT_DIR; a directory entry covers everything beneath it.
PROTECTED_PATHS = (
    ".claude/hooks",            # guard.py + secret_scan.py, the two halves of the gate
    ".claude/settings.json",    # what routes tool calls into this file at all
    ".githooks",                # pre-commit + pre-push
    ".gitleaks.toml",           # the ruleset the scan actually applies
)
# Tools that write files. NotebookEdit carries its path under a different key.
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# Commands that create a commit, and so must be scanned first (T0.3).
COMMIT_CMD = re.compile(r"\bgit\s+(?:-\S+\s+)*commit\b")
COMMIT_SAFE = re.compile(r"--dry-run|--amend\s+--no-edit\b.*--dry-run")

PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCANNER = os.path.join(PROJECT_DIR, ".claude", "hooks", "secret_scan.py")


def scan_staged_or_deny(deny):
    """Run the shared secret scanner over staged content. Fail-closed on any error.

    Same script the git pre-commit hook runs, so the two layers cannot drift apart.
    """
    if not os.path.isfile(SCANNER):
        deny("Secret scanner missing at {} - commit blocked (fail-closed). "
             "Restore it before committing.".format(SCANNER))
    try:
        run = subprocess.run([sys.executable or "python3", SCANNER], cwd=PROJECT_DIR,
                             capture_output=True, text=True, timeout=180)
    except Exception as exc:
        deny("Secret scanner could not run ({}) - commit blocked (fail-closed).".format(exc))
        return
    if run.returncode != 0:
        # stderr is already redacted by the scanner: file:line + rule + masked token.
        deny("Secret scan FAILED - commit blocked.\n" + (run.stderr or "").strip()[:2000])


def protected_hit(path):
    """Return the PROTECTED_PATHS entry this path falls under, or None.

    realpath first, so a symlink planted inside the repo cannot be used to reach the gate
    from a path that looks innocent. A relative path resolves against PROJECT_DIR, which is
    what the tools themselves do.
    """
    if not path or not isinstance(path, str):
        return None
    try:
        target = os.path.realpath(os.path.join(PROJECT_DIR, os.path.expanduser(path)))
        rel = os.path.relpath(target, os.path.realpath(PROJECT_DIR))
    except (ValueError, OSError):
        return None
    if rel.startswith(os.pardir):
        return None                                     # outside the repo, not ours to judge
    rel = rel.replace(os.sep, "/")
    for guarded in PROTECTED_PATHS:
        if rel == guarded or rel.startswith(guarded + "/"):
            return guarded
    return None


def decision(kind, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": kind,                     # "ask" | "deny" | "allow"
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        # R3 hardening: on malformed/unreadable input, don't silently allow — ASK the operator
        # (fail-safer without bricking the session, which a hard deny would risk).
        decision("ask", "Guard could not parse the tool call — approve only if you trust it.")
    tool = data.get("tool_name")
    tool_input = data.get("tool_input") or {}

    if tool in WRITE_TOOLS:
        for key in ("file_path", "notebook_path"):
            guarded = protected_hit(tool_input.get(key))
            if guarded:
                decision("deny", "Blocked: {} is part of the secret gate ({}). No agent "
                                 "edits the thing that checks its work. If this change is "
                                 "genuinely needed, the operator makes it in their own "
                                 "editor.".format(tool_input.get(key), guarded))
        sys.exit(0)

    if tool != "Bash":
        sys.exit(0)
    cmd = tool_input.get("command", "") or ""

    def deny(reason):
        decision("deny", reason)

    for pat in COMMIT_BYPASS:
        if re.search(pat, cmd, re.I):
            deny("Blocked: that would bypass the pre-commit secret gate ({}). "
                 "Fix the finding instead; only the operator bypasses, in their own "
                 "shell.".format(pat))
    if COMMIT_CMD.search(cmd) and not COMMIT_SAFE.search(cmd):
        scan_staged_or_deny(deny)

    for pat in SECRETS:
        if re.search(pat, cmd, re.I):
            decision("deny", f"Blocked: looks like it would expose a secret ({pat}). "
                             f"Avoid reading/committing .env or printing tokens.")
    for pat in IRREVERSIBLE:
        if re.search(pat, cmd, re.I):
            decision("ask", f"NOMAD human gate: this looks irreversible/outward-facing "
                            f"({pat}). Approve to proceed.")
    sys.exit(0)                                         # defer to normal permissions


if __name__ == "__main__":
    main()
