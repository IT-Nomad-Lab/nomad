"""Monthly agent-docs-audit, posted to Zulip.

Runs the read-only checks from the agent-docs-audit skill across every registered project and
posts the report to `#alerts`, topic `docs-audit`. It changes nothing: every finding is a
candidate for the operator to look at (or to run the full skill on), never an automatic fix.

It always posts, even when clean, so a month with no message means the timer died.

Run once:        .venv/bin/python docs_audit.py
Print, no post:  .venv/bin/python docs_audit.py --dry-run
"""
import os
import subprocess
import sys

CHECKS = os.path.expanduser(os.environ.get(
    "NOMAD_DOCS_AUDIT_CHECKS", "~/.claude/skills/agent-docs-audit/scripts/checks.py"))
ZULIPRC = os.path.expanduser(os.environ.get("NOMAD_ZULIPRC", "~/.config/nomad/zulip-bot.zuliprc"))
CHANNEL = os.environ.get("NOMAD_DOCS_AUDIT_CHANNEL", "alerts")
TOPIC = os.environ.get("NOMAD_DOCS_AUDIT_TOPIC", "docs-audit")
MAX_BODY = 9000  # Zulip caps a message at 10,000 characters

# Lines in the report that are notes, not findings.
_NOTES = ("drift: no installed copy differs", "(memory hits are candidates")


def count_findings(report):
    """Findings per section: every unindented line under a `== name` header, minus the notes."""
    counts, section = {}, None
    for line in report.splitlines():
        if line.startswith("== "):
            section = line[3:].strip()
            counts.setdefault(section, 0)
        elif section and line.strip() and not line.startswith((" ", "\t")) \
                and not line.startswith(_NOTES):
            counts[section] += 1
    return counts


def format_message(report, counts):
    total = sum(counts.values())
    if total == 0:
        return "**Monthly docs audit:** clean, nothing to look at."
    parts = ", ".join(f"{n} {s}" for s, n in counts.items() if n)
    body = report if len(report) <= MAX_BODY else report[:MAX_BODY] + "\n… (truncated)"
    return (f"**Monthly docs audit:** {total} candidates ({parts}). Read-only; nothing was changed. "
            f"Run the agent-docs-audit skill to grade and fix.\n```text\n{body}\n```")


def run_checks():
    res = subprocess.run([sys.executable, CHECKS, "all", "--from-projects"],
                         capture_output=True, text=True, timeout=600)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"checks.py failed ({res.returncode}): {res.stderr.strip()[:500]}")
    return res.stdout.rstrip()


def send(text):
    import zulip
    client = zulip.Client(config_file=ZULIPRC)
    res = client.send_message({"type": "stream", "to": CHANNEL, "topic": TOPIC, "content": text})
    if res.get("result") != "success":
        raise RuntimeError(f"zulip send: {res.get('msg')}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    report = run_checks()
    msg = format_message(report, count_findings(report))
    print(msg)
    if "--dry-run" not in argv:
        send(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
