import docs_audit

REPORT = """== size
4.4 KB (~1116 tokens, budget 4 KB)  /home/x/nomad/CLAUDE.md
    propose: move history to docs/
6.6 KB (~1699 tokens, budget 4 KB)  /home/x/bot/CLAUDE.md
    propose: move history to docs/

== memory
/home/x/.claude/projects/p/memory/a.md
    names paths that no longer exist: ~/projects
(memory hits are candidates: a path may be gone on purpose.)

== drift
drift: no installed copy differs from its source

== worktrees
worktree /home/x/nomad-wt/ap-1 (branch feat/1) is merged into master"""


def test_counts_findings_per_section_and_skips_notes():
    assert docs_audit.count_findings(REPORT) == {"size": 2, "memory": 1, "drift": 0, "worktrees": 1}


def test_message_lists_the_counts_and_the_report():
    msg = docs_audit.format_message(REPORT, docs_audit.count_findings(REPORT))
    assert "4 candidates (2 size, 1 memory, 1 worktrees)" in msg
    assert "/home/x/bot/CLAUDE.md" in msg


def test_clean_report_still_says_so():
    clean = "== size\n\n== drift\ndrift: no installed copy differs from its source"
    msg = docs_audit.format_message(clean, docs_audit.count_findings(clean))
    assert "clean" in msg


def test_long_report_is_truncated_under_the_zulip_cap():
    long = "== size\n" + "\n".join(f"5 KB  /r/{i}/CLAUDE.md" for i in range(2000))
    msg = docs_audit.format_message(long, docs_audit.count_findings(long))
    assert len(msg) < 10000 and "(truncated)" in msg
