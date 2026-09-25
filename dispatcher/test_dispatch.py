"""Tests for the dispatcher's two safety surfaces: who may call it, and which repo it picks.

Both are the same kind of bug when they fail. A missing token check lets anything on the host
edit code; a name that silently resolves to the wrong repo edits code the operator never named.
Neither shows up as an error, which is exactly why they need tests.

Run: python3 -m pytest dispatcher/test_dispatch.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("NOMAD_DISPATCH_TOKEN", "test-token")   # before import: TOKEN is module-level

import dispatch  # noqa: E402


def _mark(tmp_path, dirname, name, extra=""):
    d = tmp_path / dirname
    d.mkdir(parents=True)
    (d / "Nomad.md").write_text(f"---\nnomad: true\nname: {name}\nlane: dev\n{extra}---\n")
    return d


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Point the registry at a throwaway tree instead of the operator's real home."""
    monkeypatch.setattr(dispatch, "ROOTS", [str(tmp_path)])
    return tmp_path


def test_exact_name_beats_the_prefix_aliases(roots):
    _mark(roots, "nomad", "NOMAD")
    _mark(roots, "nomad-crm", "NOMAD CRM")
    _mark(roots, "nomad-os", "NOMAD OS")
    # 'nomad' is the prefix alias of all three, so the loose tier is three-way ambiguous.
    # The exact tier has one, and that is the caller being specific.
    assert dispatch.resolve("NOMAD")["path"].endswith("/nomad")
    assert dispatch.resolve("nomad crm")["path"].endswith("/nomad-crm")


def test_a_short_alias_still_resolves(roots):
    _mark(roots, "crypto-trading-bot", "Crypto Trading Bot")
    assert dispatch.resolve("crypto")["path"].endswith("/crypto-trading-bot")
    assert dispatch.resolve("build me a crypto thing")["path"].endswith("/crypto-trading-bot")


def test_two_repos_with_one_name_refuse(roots):
    """A mirror or a second checkout carrying the same marker. Editing either is a guess."""
    _mark(roots, "runematch", "RuneMatch")
    _mark(roots, "runematch-copy", "RuneMatch")
    got = dispatch.resolve("RuneMatch")
    assert got.get("ambiguous"), got
    assert len(got["ambiguous"]) == 2
    body = dispatch._unresolved("RuneMatch", got)
    assert body["ok"] is False and "matches 2 projects" in body["error"]


def test_unknown_project_lists_what_exists(roots):
    _mark(roots, "shift-poc", "Shift")
    assert dispatch.resolve("nope") is None
    assert "Shift" in dispatch._unresolved("nope", None)["error"]


def test_the_same_repo_through_two_roots_is_one_project(roots, tmp_path, monkeypatch):
    """A symlinked root (how the out-of-tree Tauri shell is reached) must not double-count."""
    real = _mark(roots, "nomad-shell", "NOMAD Shell")
    other = tmp_path / "second-root"
    other.mkdir()
    os.symlink(str(real), str(other / "nomad-shell"))
    monkeypatch.setattr(dispatch, "ROOTS", [str(roots), str(other)])
    assert [r["name"] for r in dispatch.registry()] == ["NOMAD Shell"]
    assert dispatch.resolve("NOMAD Shell")["path"] == str(real)


def test_unmarked_directories_are_invisible(roots):
    (roots / "not-a-project").mkdir()
    _mark(roots, "civis-poc", "Civis")
    assert [r["name"] for r in dispatch.registry()] == ["Civis"]


def test_save_image_refuses_a_tie_rather_than_picking(roots):
    """resolve_from_text writes a file. A wrong pick is silent, so a tie must return nothing."""
    _mark(roots, "alpha", "Alpha")
    _mark(roots, "alpha-two", "Alpha")
    assert dispatch.resolve_from_text("put this in alpha") is None
    res = dispatch.save_image("put this in alpha", "x.png", "", None)
    assert res["ok"] is False and res.get("no_project")


def test_verify_refuses_a_destructive_command(roots):
    _mark(roots, "civis-poc", "Civis")
    assert dispatch.verify("Civis", "rm -rf build")["ok"] is False
    assert dispatch.verify("Civis", "git push")["ok"] is False
    assert dispatch.verify("Civis", "curl https://example.com")["ok"] is False
    assert dispatch.verify("Civis", "echo hello")["ok"] is True


# ── auth ────────────────────────────────────────────────────────────────────────────────

class _Req:
    """The smallest thing Handler._authed touches: a header bag and a captured response."""

    def __init__(self, token):
        self.headers = {"X-Nomad-Token": token} if token is not None else {}
        self.sent = None

    _send = lambda self, code, obj: setattr(self, "sent", (code, obj))
    _authed = dispatch.Handler._authed


@pytest.mark.parametrize("token", [None, "", "wrong", "test-token "])
def test_a_wrong_or_missing_token_is_refused(token):
    r = _Req(token)
    assert r._authed() is False
    assert r.sent[0] == 401


def test_the_right_token_passes():
    r = _Req(dispatch.TOKEN)
    assert r._authed() is True
    assert r.sent is None
