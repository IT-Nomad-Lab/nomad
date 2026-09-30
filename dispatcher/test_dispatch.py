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
    assert dispatch.resolve("CRYPTO")["path"].endswith("/crypto-trading-bot")


def test_free_text_containing_an_alias_does_not_resolve(roots):
    """#851. The Manager wrote prose into args.project and the substring tier picked a repo out of
    it. Whole-string matches only; the repos the text mentions come back as candidates instead."""
    _mark(roots, "nomad", "NOMAD")
    _mark(roots, "nomad-crm", "NOMAD CRM")
    _mark(roots, "crypto-trading-bot", "Crypto Trading Bot")
    for text in ("build me a crypto thing", "fix-sshd-boot.ps1 (likely rogue01/NOMAD host repo)",
                 "IT-Nomad-Lab/nomad", "claude-bridge"):
        assert dispatch.resolve(text) is None, text
    got = dispatch.check_project("fix-sshd-boot.ps1 (likely rogue01/NOMAD host repo)")
    assert got["ok"] is False and got["reason"] == "unknown" and "Unknown project" in got["error"]
    assert sorted(got["candidates"]) == ["nomad", "nomad-crm"]
    # Nothing in the text points anywhere: offer the whole list.
    assert sorted(dispatch.check_project("claude-bridge")["candidates"]) == [
        "crypto-trading-bot", "nomad", "nomad-crm"]


def test_owner_repo_matches_only_exactly(roots):
    _mark(roots, "nomad-crm", "NOMAD CRM", "repo: IT-Nomad-Lab/nomad-crm\n")
    _mark(roots, "nomad", "NOMAD", "repo: local\n")
    assert dispatch.resolve("it-nomad-lab/NOMAD-CRM")["path"].endswith("/nomad-crm")
    assert dispatch.resolve("IT-Nomad-Lab/nomad") is None
    assert dispatch.resolve("IT-Nomad-Lab/nomad-crm-old") is None
    assert dispatch.resolve("local") is None          # 'local' is not an owner/repo


def test_check_project_names_the_registry_id(roots):
    _mark(roots / "civis", "poc", "Civis")
    _registry(roots, "  - {id: civis-poc, path: civis/poc, group: civis}\n")
    got = dispatch.check_project("civis")
    assert got["ok"] is True and got["id"] == "civis-poc" and got["name"] == "Civis"


def test_check_project_lists_an_ambiguity(roots):
    _mark(roots, "runematch", "RuneMatch")
    _mark(roots, "runematch-copy", "RuneMatch")
    got = dispatch.check_project("RuneMatch")
    assert got["ok"] is False and got["reason"] == "ambiguous"
    assert sorted(got["candidates"]) == ["runematch", "runematch-copy"]


def test_the_approved_plan_reaches_the_builder_prompt(roots, monkeypatch):
    """#851: the operator approves the plan, so the Builder must be handed the plan, not the title."""
    assert dispatch.builder_prompt("Add export") == "Add export"
    prompt = dispatch.builder_prompt("Add export", "1. add a CSV route\n2. test it")
    assert prompt.startswith("Add export") and "APPROVED PLAN:\n1. add a CSV route\n2. test it" in prompt

    seen = {}

    class _Proc:
        returncode, stdout, stderr = 0, '{"result": "done"}', ""

    def run(cmd, **kw):
        seen["prompt"] = cmd[cmd.index("-p") + 1]
        return _Proc()
    monkeypatch.setattr(dispatch.subprocess, "run", run)
    dispatch.run_claude(str(roots), "Add export", "build", "", "1. add a CSV route")
    assert "1. add a CSV route" in seen["prompt"]


def test_a_folder_of_one_repo_names_that_repo(roots):
    """#906: run 50d61fe163ff sent 'claude-bridge', a folder of the nomad repo, three times."""
    nomad = _mark(roots, "nomad", "NOMAD")
    _mark(roots, "nomad-crm", "NOMAD CRM")
    (nomad / "claude-bridge").mkdir()
    (nomad / "claude-bridge" / "bridge.py").write_text("")
    for name in ("claude-bridge", "Claude-Bridge", "nomad/claude-bridge", "NOMAD/claude-bridge/",
                 str(nomad / "claude-bridge"), str(nomad / "claude-bridge" / "bridge.py")):
        got = dispatch.resolve(name)
        assert got and got["path"] == str(nomad), name
    assert dispatch.resolve("claude-bridge")["subdir"] == "claude-bridge"
    assert dispatch.resolve("nomad/claude-bridge/bridge.py")["subdir"] == "claude-bridge/bridge.py"
    got = dispatch.check_project("claude-bridge")
    assert got["ok"] is True and got["id"] == "nomad" and got["target"] == "nomad/claude-bridge"
    # The target written back into the proposal resolves to the same place again.
    assert dispatch.check_project(got["target"])["target"] == "nomad/claude-bridge"
    # A repo named plainly carries no subdir, and the repo root as a path is just the repo.
    assert "subdir" not in dispatch.resolve("nomad")
    assert dispatch.check_project(str(nomad))["target"] == "nomad"


def test_a_folder_or_path_only_resolves_when_it_exists(roots):
    """A lookup on disk, never a guess (#851): nothing that is not there resolves."""
    nomad = _mark(roots, "nomad", "NOMAD")
    crm = _mark(roots, "nomad-crm", "NOMAD CRM")
    (nomad / "claude-bridge").mkdir()
    (nomad / ".git").mkdir()
    (nomad / "README.md").write_text("")
    for name in ("codex-bridge", "nomad/codex-bridge", str(nomad / "codex-bridge"),
                 "nomad/../nomad-crm", "nomad/", ".git", "README.md", "claude-bridge please",
                 "rogue01/claude-bridge", "/nonexistent/claude-bridge"):
        assert dispatch.resolve(name) is None, name
    assert crm.exists()


def test_a_folder_in_two_repos_is_ambiguous(roots):
    a = _mark(roots, "nomad", "NOMAD")
    b = _mark(roots, "nomad-crm", "NOMAD CRM")
    (a / "scripts").mkdir()
    (b / "scripts").mkdir()
    got = dispatch.check_project("scripts")
    assert got["ok"] is False and got["reason"] == "ambiguous"
    assert sorted(got["candidates"]) == ["nomad-crm/scripts", "nomad/scripts"]
    assert dispatch.resolve("nomad-crm/scripts")["path"] == str(b)


def test_the_builder_is_told_which_folder(roots, monkeypatch):
    assert "`claude-bridge`" in dispatch.builder_prompt("Add alert", "", "claude-bridge")
    assert dispatch.builder_prompt("Add alert") == "Add alert"
    p = dispatch.builder_prompt("Add alert", "1. hook", "claude-bridge")
    assert p.index("`claude-bridge`") < p.index("APPROVED PLAN")


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


def test_a_group_root_names_its_repos_group_dash_dir(roots, monkeypatch):
    """~/civis/{poc,site}: the old flat names still resolve, and bare 'site' does not leak."""
    group = roots / "civis"
    group.mkdir()
    _mark(group, "poc", "Civis")
    _mark(group, "site", "CIVIS Site")
    _mark(roots, "itnomadlab-site", "ITNomadLab Site")
    monkeypatch.setattr(dispatch, "ROOTS", [str(roots), str(group)])
    assert dispatch.resolve("civis-poc")["name"] == "Civis"
    assert dispatch.resolve("civis-site")["name"] == "CIVIS Site"
    assert "site" not in dispatch.resolve("civis-site")["aliases"]
    assert dispatch.resolve_from_text("save it in itnomadlab-site")["name"] == "ITNomadLab Site"


def test_a_former_name_in_aliases_resolves_exactly(roots, monkeypatch):
    """~/content-creation became a group; its content repo keeps the old name via `aliases:`."""
    group = roots / "content-creation"
    group.mkdir()
    _mark(group, "content", "Content Creation", "aliases: [content-creation]\n")
    _mark(group, "app", "Content Creation App")
    monkeypatch.setattr(dispatch, "ROOTS", [str(roots), str(group)])
    assert dispatch.resolve("content-creation")["name"] == "Content Creation"
    assert dispatch.resolve("content-creation-app")["name"] == "Content Creation App"


def _registry(roots, body):
    (roots / "nomad").mkdir(exist_ok=True)
    (roots / "nomad" / "projects.yaml").write_text("projects:\n" + body)


def test_the_registry_file_is_the_list_when_present(roots):
    """projects.yaml decides: an unlisted marker is invisible, a dispatch:false entry is
    never a target, and a listed folder that is missing (the console cannot see /mnt/c) is
    skipped rather than an error."""
    _mark(roots, "nomad", "NOMAD")
    _mark(roots / "civis", "poc", "Civis")
    _mark(roots, "nomad-public", "NOMAD")
    _mark(roots, "stray", "Stray")
    _registry(roots, "  - {id: nomad, path: nomad}\n"
                     "  - {id: civis-poc, path: civis/poc, group: civis}\n"
                     "  - {id: nomad-public, path: nomad-public, dispatch: false}\n"
                     "  - {id: gone, path: not-here}\n")
    assert sorted(r["slug"] for r in dispatch.registry()) == ["civis-poc", "nomad"]
    assert dispatch.resolve("civis-poc")["name"] == "Civis"
    # nomad-public carries the same name as nomad; with it excluded, NOMAD is not ambiguous.
    assert dispatch.resolve("NOMAD")["path"].endswith("/nomad")
    assert dispatch.resolve("stray") is None
    assert [e["id"] for e in dispatch.load_projects()][-2:] == ["nomad-public", "gone"]


def test_registry_aliases_resolve_exactly(roots):
    _mark(roots / "content-creation", "content", "Content Creation")
    _registry(roots, "  - {id: content-creation-content, path: content-creation/content,"
                     " aliases: [content-creation]}\n")
    assert dispatch.resolve("content-creation")["name"] == "Content Creation"


def test_scaffold_appends_to_the_registry(roots):
    _mark(roots, "nomad", "NOMAD")
    _registry(roots, "  # hub\n  - {id: nomad, path: nomad}\n")
    res = dispatch.new_project("Tiny Tool")
    assert res["ok"], res
    text = (roots / "nomad" / "projects.yaml").read_text()
    assert "# hub" in text and text.endswith("  - {id: tiny-tool, path: tiny-tool}\n")
    assert dispatch.resolve("tiny-tool")["name"] == "Tiny Tool"
    assert dispatch.register_project("tiny-tool", res["path"]) is False   # no duplicate


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


# ── feature branches ────────────────────────────────────────────────────────────────────
# Operator rule: a build never edits the default branch. A regression here is silent: the
# Builder would just edit master again, and nothing would look wrong until the diff landed.

def _repo(tmp_path, default="main"):
    import subprocess
    d = tmp_path / "repo"
    d.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)
    run("init", "-q", "-b", default)
    (d / "README.md").write_text("x\n")
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return str(d)


@pytest.mark.parametrize("default", ["main", "master"])
def test_a_build_on_the_default_branch_moves_to_a_feature_branch(tmp_path, default):
    path = _repo(tmp_path, default)
    branch, err = dispatch.ensure_feature_branch(path, "Add a CSV export to the report page")
    assert err is None
    assert branch == "feat/add-a-csv-export-to-the"
    assert dispatch.git(path, "branch", "--show-current") == branch


def test_a_taken_branch_name_gets_a_suffix(tmp_path):
    path = _repo(tmp_path)
    dispatch.git(path, "branch", "feat/fix-login")
    branch, _ = dispatch.ensure_feature_branch(path, "fix login")
    assert branch == "feat/fix-login-2"


def test_a_feature_branch_in_progress_is_kept(tmp_path):
    path = _repo(tmp_path)
    dispatch.git(path, "switch", "-q", "-c", "feat/ongoing")
    branch, err = dispatch.ensure_feature_branch(path, "next step of the feature")
    assert (branch, err) == ("feat/ongoing", None)


def test_an_explicit_branch_is_created_or_reused(tmp_path):
    path = _repo(tmp_path)
    assert dispatch.ensure_feature_branch(path, "t", "feat/named")[0] == "feat/named"
    dispatch.git(path, "switch", "-q", "main")
    assert dispatch.ensure_feature_branch(path, "t", "feat/named")[0] == "feat/named"
    assert dispatch.git(path, "branch", "--show-current") == "feat/named"


def test_branch_none_is_the_operators_opt_out(tmp_path):
    path = _repo(tmp_path)
    assert dispatch.ensure_feature_branch(path, "tiny fix", "none") == ("", None)
    assert dispatch.git(path, "branch", "--show-current") == "main"


def test_a_folder_without_git_is_left_alone(tmp_path):
    assert dispatch.ensure_feature_branch(str(tmp_path), "anything") == ("", None)


def test_a_detached_head_refuses(tmp_path):
    path = _repo(tmp_path)
    dispatch.git(path, "switch", "-q", "--detach")
    branch, err = dispatch.ensure_feature_branch(path, "anything")
    assert branch is None and "detached" in err


def test_the_claude_fallback_is_the_native_installer_path():
    """#861: the linuxbrew fallback was gone, and every build failed on it."""
    src = open(dispatch.__file__).read()
    line = next(l for l in src.splitlines() if l.startswith("CLAUDE ="))
    assert "~/.local/bin/claude" in line and "linuxbrew" not in line


def test_claude_found_says_whether_the_binary_is_there(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    monkeypatch.setattr(dispatch, "CLAUDE", str(exe))
    assert not dispatch.claude_found()
    exe.write_text("#!/bin/sh\n")
    assert not dispatch.claude_found()          # there, but not runnable
    exe.chmod(0o755)
    assert dispatch.claude_found()


def test_the_unit_puts_local_bin_on_path():
    unit = open(os.path.join(os.path.dirname(dispatch.__file__), "nomad-dispatch.service")).read()
    path = next(l for l in unit.splitlines() if l.startswith("Environment=PATH="))
    assert path.split("=", 2)[2].split(":")[0] == "/home/jdelimam/.local/bin"
