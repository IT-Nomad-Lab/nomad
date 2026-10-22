"""#1115 · project context for an Open WebUI project chat (docs/prd/owui-sessions.md §6.2 step 1).

Run: python3 -m pytest v2/test_owui_context.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import owui_context as C  # noqa: E402

CTX = {"id": "civis-poc", "name": "Civis", "board": "civis",
       "claude_md": {"path": "CLAUDE.md", "text": "# Civis rules", "truncated": False},
       "prd": {"path": "docs/prd/x.md", "text": "PRD body", "truncated": False}}


class Search:
    def __init__(self):
        self.calls = []

    def __call__(self, project="", limit=25, **f):
        self.calls.append((project, f))
        return f"#{len(self.calls)} [Task] {f} · {project}"


@pytest.fixture(autouse=True)
def fresh_cache():
    C._cache.clear()


def test_the_block_has_both_docs_and_the_board_with_ids():
    s = Search()
    out = C.build("civis-poc", lambda pid: CTX, s)
    assert "PROJECT: Civis (registry id civis-poc, nomad-pm board civis)" in out
    assert "=== CLAUDE.md (CLAUDE.md) ===\n# Civis rules" in out
    assert "=== PRD (docs/prd/x.md) ===\nPRD body" in out
    assert [p for p, _ in s.calls] == ["civis"] * 4
    assert [f for _, f in s.calls] == [{"type": "Epic"}, {"status": "In Progress"},
                                       {"status": "Ready"}, {"status": "Blocked"}]
    assert "Open Epics:\n#1 [Task]" in out and "Blocked:\n#4 [Task]" in out


def test_each_part_is_clipped_to_its_share(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_TOKENS", 100)            # 400 chars: CLAUDE.md gets 160
    big = {**CTX, "claude_md": {"path": "CLAUDE.md", "text": "x" * 5000}}
    out = C.build("civis-poc", lambda pid: big, Search())
    section = out.split("=== CLAUDE.md (CLAUDE.md) ===\n")[1].split("\n\n===")[0]
    assert len(section) <= 160 and "[clipped, 4840 more chars]" in section


def test_missing_parts_say_so():
    out = C.build("civis-poc", lambda pid: {**CTX, "prd": None}, Search())
    assert "=== PRD ===\n(not available)" in out
    def down(pid):
        raise OSError("dispatcher down")
    out = C.build("civis-poc", down, Search())
    assert "(not available: OSError: dispatcher down)" in out
    assert "nomad-pm board civis-poc" in out                # best guess: the id is the board


def test_a_project_without_a_board_skips_nomad_pm():
    s = Search()
    out = C.build("pub", lambda pid: {**CTX, "board": None}, s)
    assert "(this project has no board)" in out and s.calls == []


def test_a_failing_pm_search_is_reported_in_place():
    def bad(**k):
        raise RuntimeError("OpenProject 502")
    assert "Ready:\n(unavailable: RuntimeError: OpenProject 502)" in C.build("x", lambda p: CTX, bad)


def test_cached_per_chat_for_five_minutes():
    t = [1000.0]
    s = Search()
    a = C.load("chat1", "civis-poc", lambda pid: CTX, s, now=lambda: t[0])
    t[0] += 299
    assert C.load("chat1", "civis-poc", lambda pid: CTX, s, now=lambda: t[0]) == a
    assert len(s.calls) == 4
    C.load("chat2", "civis-poc", lambda pid: CTX, s, now=lambda: t[0])   # another chat: its own
    assert len(s.calls) == 8
    t[0] += 2
    C.load("chat1", "civis-poc", lambda pid: CTX, s, now=lambda: t[0])   # expired
    assert len(s.calls) == 12


def test_a_smaller_budget_clips_harder_and_is_cached_apart():
    big = {**CTX, "claude_md": {"path": "CLAUDE.md", "text": "x" * 20000}}
    small = C.build("civis-poc", lambda pid: big, Search(), budget_tokens=1000)
    assert "[clipped, 18400 more chars]" in small               # 4000 chars × 0.4 = 1600
    s = Search()
    C.load("c", "civis-poc", lambda pid: big, s, budget_tokens=1000)
    C.load("c", "civis-poc", lambda pid: big, s, budget_tokens=8000)
    assert len(s.calls) == 8
