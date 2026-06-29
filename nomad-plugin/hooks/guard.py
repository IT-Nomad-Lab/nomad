#!/usr/bin/env python3
"""NOMAD v2 build-team guardrail (PreToolUse hook).

Routes IRREVERSIBLE actions to the operator for approval and flags SECRET exposure, so the
human gate is enforced by the harness — not by an agent remembering to ask. Scoped to Bash
in .claude/settings.json. Decisions:
  - irreversible pattern  -> "ask"  (operator confirms)
  - secret-exfil pattern  -> "deny" (blocked; reword)
  - otherwise             -> defer to normal permissions (exit 0, no decision)

Fail-open by design (a buggy guard must not brick the session); the security-reviewer agent
hardens this and the risk register tracks it (R3).
"""
import json
import re
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
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = (data.get("tool_input") or {}).get("command", "") or ""
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
