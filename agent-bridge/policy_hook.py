#!/usr/bin/env python3
"""PreToolUse hook the bridge passes to every Claude Code session (`--settings`).

Enforces requirement 9 for chats that have no project guard of their own:
- `git stash` anywhere in the NOMAD tree: denied (the stash stack is shared by every worktree).
- the cockpit token and the guardrail save (#1396), in a command or in any tool's path or URL:
  denied. A guardrail is saved only by the operator, from the cockpit.
- `git commit` in the shared ~/nomad checkout without `--only`: denied.
- push, deploy, delete, destructive docker/sql: "ask", which the bridge shows in the chat as an
  Approve/Deny with a run id. Force-push is denied outright.
Reads the hook JSON on stdin, writes the decision JSON on stdout. Fail-open on a parse error:
the project's own guard.py and Claude Code's permission rules still apply.
"""
import json
import os
import re
import sys

HOME = os.path.expanduser("~")
SHARED = os.path.join(HOME, "nomad")

STASH = re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+stash\b(?!\s+(?:list|show)\b)")
COMMIT = re.compile(r"\bgit\b(?:\s+-C\s+(\S+))?\s+commit\b")
COCKPIT = re.compile(r"cockpit[-_]token|guardrail[-_]save|X-Nomad-Cockpit", re.I)
PATH_KEYS = ("file_path", "path", "notebook_path", "url", "pattern", "glob")
FORCE = re.compile(r"\bgit\b.*\bpush\b.*(?:\s--force\b|\s-f\b|--force-with-lease|--mirror)")
ASK = [
    (re.compile(r"\bgit\b.*\bpush\b"), "git push"),
    (re.compile(r"\bgh\s+(pr|release|repo)\s+(create|merge|delete)\b"), "GitHub PR/release/repo change"),
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+"), "recursive or forced delete"),
    (re.compile(r"\bgit\b.*\b(reset\s+--hard|clean\s+-[a-z]*f|branch\s+-D|worktree\s+remove)\b"), "destructive git"),
    (re.compile(r"\bdocker\s+(compose\s+down\s+.*-v|volume\s+rm|system\s+prune|rm\b)"), "docker delete"),
    (re.compile(r"\bdocker\s+compose\s+(up|build)\b"), "docker deploy"),
    (re.compile(r"\bsystemctl\b.*\b(restart|stop|disable|enable|start)\b"), "service change"),
    (re.compile(r"\b(drop|truncate)\s+(table|database)\b|\bdelete\s+from\b", re.I), "database delete"),
    (re.compile(r"\btailscale\s+(funnel|serve)\b"), "network exposure"),
    (re.compile(r"\b(deploy|publish|kubectl\s+(apply|delete)|npm\s+publish|twine\s+upload)\b"), "deploy/publish"),
]


def decide(tool: str, tin: dict, cwd: str) -> tuple[str, str]:
    """("allow"|"ask"|"deny", reason)."""
    if tool != "Bash":
        reach = json.dumps(tin) if tool.startswith("mcp__") else " ".join(str(tin.get(k) or "") for k in PATH_KEYS)
        if COCKPIT.search(reach):
            return "deny", "the cockpit token and the guardrail save are the operator's alone."
        return "allow", ""
    cmd = str(tin.get("command") or "")
    if COCKPIT.search(cmd):
        return "deny", "the cockpit token and the guardrail save are the operator's alone."
    if STASH.search(cmd):
        return "deny", ("git stash is never used in the NOMAD tree: the stash stack is shared by "
                        "every worktree. Commit a WIP commit on your branch instead.")
    for m in COMMIT.finditer(cmd):
        where = os.path.realpath(os.path.expanduser(m.group(1) or cwd or "."))
        if where == SHARED and "--only" not in cmd and " -o " not in cmd:
            return "deny", ("commits in the shared ~/nomad checkout must use `git commit --only "
                            "<paths>`; better, work in a worktree on your own branch.")
    if FORCE.search(cmd):
        return "deny", "force-push is never allowed."
    for rx, why in ASK:
        if rx.search(cmd):
            return "ask", f"needs the operator's approval: {why}"
    return "allow", ""


def main() -> int:
    try:
        data = json.load(sys.stdin)
        verdict, why = decide(data.get("tool_name", ""), data.get("tool_input") or {}, data.get("cwd") or "")
    except Exception:
        return 0
    if verdict != "allow":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                  "permissionDecision": verdict,
                                                  "permissionDecisionReason": why}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
