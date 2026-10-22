"""Tests for the dispatcher's two safety surfaces: who may call it, and which repo it picks.

Both are the same kind of bug when they fail. A missing token check lets anything on the host
edit code; a name that silently resolves to the wrong repo edits code the operator never named.
Neither shows up as an error, which is exactly why they need tests.

Run: python3 -m pytest dispatcher/test_dispatch.py -q
"""
import json
import os
import subprocess
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
        seen["model"], seen["env"] = cmd[cmd.index("--model") + 1], kw["env"]
        return _Proc()
    monkeypatch.setattr(dispatch.subprocess, "run", run)
    dispatch.run_claude(str(roots), "Add export", "build", "", "1. add a CSV route")
    assert "1. add a CSV route" in seen["prompt"]
    # #1089: Sonnet by default, the Max plan, 1M window off, subagents on Sonnet
    assert seen["model"] == "sonnet" and seen["env"]["ANTHROPIC_API_KEY"] == ""
    assert (seen["env"]["CLAUDE_CODE_DISABLE_1M_CONTEXT"], seen["env"]["CLAUDE_CODE_SUBAGENT_MODEL"]) == ("1", "sonnet")


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


def test_projects_payload_carries_local_only(roots):
    """Open WebUI keeps a local_only project's chats on local models (#1114). Only a literal
    true counts; a missing or odd value is false."""
    _mark(roots, "nomad", "NOMAD")
    _mark(roots, "private", "Private")
    _mark(roots, "odd", "Odd")
    _registry(roots, "  - {id: nomad, path: nomad}\n"
                     "  - {id: private, path: private, local_only: true}\n"
                     "  - {id: odd, path: odd, local_only: 'yes'}\n")
    d = dispatch.projects_payload()
    assert d["ids"] == ["nomad", "private", "odd"]
    assert {r["id"]: r["local_only"] for r in d["detail"]} == {"nomad": False, "private": True,
                                                              "odd": False}
    assert {r["id"]: r["board"] for r in d["detail"]}["nomad"] == "nomad-work"   # BOARDS


def test_the_project_type_reaches_the_dispatcher_views(roots):
    """`type:` in projects.yaml is read by the dispatcher (#1369): missing = generic, and it
    shows in /projects detail and /project-context. Nothing else about a project changes."""
    _mark(roots, "plain", "Plain")
    _mark(roots, "site", "Site")
    _registry(roots, "  - {id: plain, path: plain}\n  - {id: site, path: site, type: website}\n")
    assert [r["type"] for r in dispatch.registry()] == ["generic", "website"]
    assert {r["id"]: r["type"] for r in dispatch.projects_payload()["detail"]} == {
        "plain": "generic", "site": "website"}
    assert dispatch.project_context("plain")["type"] == "generic"
    assert dispatch.project_context("site")["type"] == "website"


def test_project_context_reads_claude_md_and_the_prd(roots, monkeypatch):
    """The Open WebUI sessions read a project's CLAUDE.md and PRD through here (#1115)."""
    import time
    d = _mark(roots, "civis", "Civis")
    (d / "CLAUDE.md").write_text("# Civis\nrules")
    (d / "docs" / "prd").mkdir(parents=True)
    (d / "docs" / "prd" / "old.md").write_text("old")
    time.sleep(0.01)
    (d / "docs" / "prd" / "new.md").write_text("new prd")
    _registry(roots, "  - {id: civis-poc, path: civis}\n")
    c = dispatch.project_context("civis-poc")
    assert c["board"] == "civis" and c["name"] == "Civis"
    assert c["claude_md"] == {"path": "CLAUDE.md", "text": "# Civis\nrules", "truncated": False}
    assert c["prd"]["path"] == "docs/prd/new.md" and c["prd"]["text"] == "new prd"
    (d / "docs" / "PRD.md").write_text("the PRD")                 # docs/PRD.md wins
    assert dispatch.project_context("civis-poc")["prd"]["text"] == "the PRD"
    monkeypatch.setattr(dispatch, "CONTEXT_MAX", 5)
    assert dispatch.project_context("civis-poc")["claude_md"] == {
        "path": "CLAUDE.md", "text": "# Civ", "truncated": True}


def test_project_context_only_answers_for_registry_ids(roots):
    d = _mark(roots, "bare", "Bare")
    _registry(roots, "  - {id: bare, path: bare}\n  - {id: off, path: bare, dispatch: false}\n")
    c = dispatch.project_context("bare")
    assert c["claude_md"] is None and c["prd"] is None
    assert dispatch.project_context("off") is None
    assert dispatch.project_context("../etc") is None
    assert dispatch.project_context("") is None


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


# ── + New project from Open WebUI (#1152, docs/prd/owui-sessions.md §6.4 steps 3.1, 3.3) ──

def test_the_full_scaffold_has_what_an_existing_project_has(roots):
    _mark(roots, "nomad", "NOMAD")
    _registry(roots, "  - {id: nomad, path: nomad}\n")
    res = dispatch.new_project("Tide Log", "Tides for weekend sailors.", id="tide-log",
                               prd="# PRD: Tide Log\n\n## Problem and why now\nx",
                               stack=["python", "fastapi"], local_only=True, register=False)
    assert res["ok"] and res["slug"] == "tide-log", res
    repo = roots / "tide-log"
    for f in ("Nomad.md", "CLAUDE.md", "AGENTS.md", "README.md", ".gitignore", ".mcp.json",
              "docs/PRD.md"):
        assert (repo / f).is_file(), f
    nomad_md = (repo / "Nomad.md").read_text()
    for field in ("nomad: true", "name: Tide Log", "status: planning", "owner: ", 'repo: ""',
                  "lane: dev", "stack: [python, fastapi]", "updated: "):
        assert field in nomad_md, field
    assert "docs/SHARED-RESOURCES.md" in nomad_md
    for f in ("CLAUDE.md", "AGENTS.md"):
        assert "docs/SDLC.md" in (repo / f).read_text()
    assert "local-only" in (repo / "CLAUDE.md").read_text()
    mcp = json.loads((repo / ".mcp.json").read_text())["mcpServers"]
    assert sorted(mcp) == sorted(dispatch.SHARED_MCP)
    assert mcp["nomad-local"]["args"][0].endswith("mcp-local/nomad_local.py")
    assert dispatch.git(str(repo), "log", "--oneline") and not dispatch.git(str(repo), "status", "--short")
    assert "tide-log" not in (roots / "nomad" / "projects.yaml").read_text()      # register: false
    again = dispatch.new_project("Tide Log", id="tide-log", register=False)
    assert again["ok"] and again["existed"]                                      # re-run = done
    assert dispatch.new_project("Tide Log")["ok"] is False                       # console form
    assert dispatch.new_project("x", id="Bad Id")["ok"] is False


def _pm_repo(tmp_path, monkeypatch, seed_out):
    """A fake ~/nomad-pm on main: seed.rb with a BOARDS block, seed.sh that prints what the real
    one logs for each board."""
    repo = tmp_path / "nomad-pm"
    repo.mkdir()
    (repo / "seed.rb").write_text('x = 1\nBOARDS = [\n  ["nomad-work", "NOMAD work"], ["civis", "Civis"],\n'
                                  '  ["runematch", "RuneMatch"]\n].freeze\ny = 2\n')
    (repo / "seed.sh").write_text(
        "#!/usr/bin/env bash\ncd \"$(dirname \"$0\")\"\n"
        "grep -o '\\[\"[a-z0-9-]*\", ' seed.rb | tr -dc 'a-z0-9-\\n' | nl -w1 -s' ' | "
        "while read n b; do echo \"[seed] board: $b id=$n, 4 members\"; done\n"
        f"echo ran >> {seed_out}\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    monkeypatch.setattr(dispatch, "PM_REPO", str(repo))
    return repo


def test_seed_board_adds_the_row_on_a_branch_seeds_and_merges(tmp_path, monkeypatch):
    log = tmp_path / "seeded"
    repo = _pm_repo(tmp_path, monkeypatch, log)
    res = dispatch.seed_board("tide-log", "Tide Log")
    assert res["ok"] and res["merged"] and res["op_id"] == 4, res
    text = (repo / "seed.rb").read_text()
    assert '  ["runematch", "RuneMatch"],\n  ["tide-log", "Tide Log"]\n].freeze\ny = 2\n' in text
    assert "board/tide-log" not in dispatch.git(str(repo), "branch")
    assert not (tmp_path / "nomad-pm-wt" / "board-tide-log").exists()
    again = dispatch.seed_board("tide-log", "Tide Log")                 # already in main: re-seed
    assert again["ok"] and again["merged"] and again["branch"] == ""
    assert (repo / "seed.rb").read_text().count("tide-log") == 1
    assert log.read_text().count("ran") == 2


def test_seed_board_never_touches_a_dirty_main_checkout(tmp_path, monkeypatch):
    """Someone's uncommitted seed.rb edit stays as it is: the board is seeded from the branch,
    and the branch waits to be merged."""
    repo = _pm_repo(tmp_path, monkeypatch, tmp_path / "seeded")
    with open(repo / "seed.rb", "a") as f:
        f.write("# someone's work in progress\n")
    res = dispatch.seed_board("tide-log", "Tide Log")
    assert res["ok"] and not res["merged"] and res["branch"] == "board/tide-log", res
    assert "isn't merged" in res["note"] and "uncommitted changes to seed.rb" in res["note"]
    assert (repo / "seed.rb").read_text().endswith("# someone's work in progress\n")
    assert "tide-log" not in (repo / "seed.rb").read_text()
    assert dispatch.seed_board("tide-log", "Tide Log")["ok"]           # resumable from the branch


def test_seed_board_refuses_bad_input(tmp_path, monkeypatch):
    _pm_repo(tmp_path, monkeypatch, tmp_path / "seeded")
    assert not dispatch.seed_board("Bad Id", "x")["ok"]
    assert not dispatch.seed_board("ok-id", 'a "quoted" name')["ok"]
    assert not dispatch.seed_board("ok-id", "#{system('x')}")["ok"]


# ── + New project: the hub registry and the Zulip stream (#1153, §6.4 steps 3.2, 3.5) ──────

def _hub(roots, monkeypatch, checkout=True):
    """A fake ~/nomad on master: projects.yaml and a gen-workspace.py that writes the workspace."""
    hub = roots / "hub"
    (hub / "scripts").mkdir(parents=True)
    (hub / "projects.yaml").write_text("projects:\n  - id: nomad\n    path: nomad\n")
    (hub / "scripts" / "gen-workspace.py").write_text(       # folders that exist under the home
        "import json, os, re\nhome = os.environ['NOMAD_WORKSPACE_HOME']\n"
        "ids = re.findall(r'- id: (\\S+)', open('projects.yaml').read())\n"
        "ids = [i for i in ids if os.path.isdir(os.path.join(home, i))]\n"
        "json.dump({'folders': ids}, open('nomad.code-workspace', 'w'))\n")
    (hub / "nomad.code-workspace").write_text('{"folders": ["nomad"]}')
    (roots / "nomad").mkdir(exist_ok=True)
    for args in (["init", "-q", "-b", "master"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(hub), *args], check=True)
    if not checkout:                                    # another session switched the checkout
        subprocess.run(["git", "-C", str(hub), "checkout", "-q", "-b", "other"], check=True)
    monkeypatch.setattr(dispatch, "HUB", str(hub))
    monkeypatch.setattr(dispatch, "PY", [sys.executable])
    dispatch.new_project("Tide Log", id="tide-log", register=False)
    return hub


@pytest.mark.parametrize("checkout", [True, False])
def test_register_hub_adds_the_entry_on_a_branch_and_merges(roots, monkeypatch, checkout):
    hub = _hub(roots, monkeypatch, checkout)
    res = dispatch.register_hub("tide-log", local_only=True)
    assert res["ok"] and res["merged"], res
    reg = dispatch.git(str(hub), "show", "master:projects.yaml")
    assert reg.endswith("  - id: tide-log\n    path: tide-log\n    local_only: true")
    ws = json.loads(dispatch.git(str(hub), "show", "master:nomad.code-workspace"))
    assert ws == {"folders": ["nomad", "tide-log"]}
    assert "register/tide-log" not in dispatch.git(str(hub), "branch")
    assert not dispatch.git(str(hub), "status", "--short")
    assert dispatch.register_hub("tide-log")["note"] == "already registered"


def test_register_hub_leaves_a_dirty_master_alone(roots, monkeypatch):
    hub = _hub(roots, monkeypatch)
    with open(hub / "projects.yaml", "a") as f:
        f.write("  # someone's work in progress\n")
    res = dispatch.register_hub("tide-log")
    assert res["ok"] and not res["merged"] and res["branch"] == "register/tide-log", res
    assert (hub / "projects.yaml").read_text().endswith("# someone's work in progress\n")
    assert dispatch.register_hub("tide-log")["ok"]                     # resumable from the branch


def test_register_hub_refuses_what_it_cant_register(roots, monkeypatch):
    _hub(roots, monkeypatch)
    assert not dispatch.register_hub("Bad Id")["ok"]
    assert "no scaffolded repo" in dispatch.register_hub("not-made")["error"]


def test_the_autopilot_entry_passes_the_permission_check_and_never_passes_tests(tmp_path):
    from autopilot import permissions, projects
    reg = tmp_path / "projects.yaml"
    reg.write_text("projects:\n" + dispatch._registry_entry("tide-log", "tide-log", False, True))
    p = projects.load(str(reg), base=str(tmp_path))[0]
    assert p.autopilot
    permissions.build_settings(p, os.path.join(p.autopilot.worktree_root, "ap-1"), state_dir=str(tmp_path / "s"),
                               default_branch="master")
    assert subprocess.run(["bash", "-c", dispatch.NO_TESTS], capture_output=True).returncode == 1


def test_project_stream_runs_the_relay_helper(tmp_path, monkeypatch):
    relay = tmp_path / "relay"
    (relay / ".venv" / "bin").mkdir(parents=True)
    py = relay / ".venv" / "bin" / "python"
    py.write_text("#!/usr/bin/env bash\necho \"$NOMAD_ZULIP_OPERATOR_ID\" > seen\necho \"#$2: ok\"\n")
    py.chmod(0o755)
    env = tmp_path / "relay.env"
    env.write_text('NOMAD_ZULIP_OPERATOR_ID="8"\nOTHER_SECRET=x\n')
    monkeypatch.setattr(dispatch, "RELAY", str(relay))
    monkeypatch.setattr(dispatch, "RELAY_ENV", str(env))
    assert dispatch.project_stream("tide-log") == {"ok": True, "stream": "tide-log", "result": "ok"}
    assert (relay / "seen").read_text().strip() == "8"
    assert not dispatch.project_stream("Bad Id")["ok"]
    monkeypatch.setattr(dispatch, "RELAY_ENV", str(tmp_path / "missing"))
    assert "can't read" in dispatch.project_stream("tide-log")["error"]


def test_register_hub_refuses_a_workspace_that_loses_folders(roots, monkeypatch):
    hub = _hub(roots, monkeypatch)
    (hub / "nomad.code-workspace").write_text('{"folders": ["nomad", "gone-a", "gone-b"]}')
    subprocess.run(["git", "-C", str(hub), "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                    "-qam", "ws"], check=True)
    res = dispatch.register_hub("tide-log")
    assert not res["ok"] and "dropped folders" in res["error"], res
    assert "tide-log" not in dispatch.git(str(hub), "show", "master:projects.yaml")


# ── delete a project from Open WebUI (#1200, docs/prd/owui-sessions.md §6.6) ───────────────

def _doomed(roots, monkeypatch, remote=False):
    """tide-log made and registered in the fake hub, with a golden file, ready to delete."""
    hub = _hub(roots, monkeypatch)
    assert dispatch.register_hub("tide-log", autopilot=False)["ok"]
    golden = hub / "autopilot" / "testdata" / "golden" / "tide-log-settings.json"
    golden.parent.mkdir(parents=True)
    golden.write_text("{}")
    subprocess.run(["git", "-C", str(hub), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(hub), "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                    "-qm", "golden"], check=True)
    monkeypatch.setattr(dispatch, "ARCHIVE", str(roots / "archive"))
    monkeypatch.setattr(dispatch, "_board_for", lambda: (lambda pid: pid))
    # the registry the dispatcher reads is the hub checkout's own file
    monkeypatch.setattr(dispatch, "REGISTRY_FILE", str(hub / "projects.yaml"))
    if remote:
        subprocess.run(["git", "-C", str(roots / "tide-log"), "remote", "add", "origin",
                        "git@github.com:x/tide-log.git"], check=True)
    return hub


def test_project_info_lists_what_a_delete_would_touch_and_refuses_hubs(roots, monkeypatch):
    _doomed(roots, monkeypatch, remote=True)
    info = dispatch.project_info("tide-log")
    assert info["known"] and info["refuse"] == "" and info["golden"] is True
    assert info["path"] == str(roots / "tide-log") and info["remote"] == ["git@github.com:x/tide-log.git"]
    assert info["archive"] == str(roots / "archive" / "tide-log")
    assert "hub repo" in dispatch.project_info("nomad")["refuse"]
    assert dispatch.project_info("nomad-pm")["refuse"] or not dispatch.project_info("nomad-pm")["known"]
    assert dispatch.project_info("nope")["known"] is False
    assert not dispatch.project_info("Bad Id")["ok"]
    monkeypatch.setattr(dispatch, "_board_for", lambda: (lambda pid: "shared"))
    assert "shares the board" in dispatch.project_info("tide-log")["refuse"]


def test_archive_project_moves_the_repo_never_erases_it_and_is_safe_to_rerun(roots, monkeypatch):
    _doomed(roots, monkeypatch)
    res = dispatch.archive_project("tide-log")
    assert res["ok"] and res["path"] == str(roots / "archive" / "tide-log") and not res["existed"]
    assert not (roots / "tide-log").exists() and (roots / "archive" / "tide-log" / "Nomad.md").is_file()
    again = dispatch.archive_project("tide-log")
    assert again["ok"] and again["existed"]                     # moved already: still ok, nothing moved


def test_archive_project_refuses_hubs_remotes_unknowns_and_an_occupied_archive(roots, monkeypatch):
    _doomed(roots, monkeypatch, remote=True)
    assert "hub repo" in dispatch.archive_project("nomad")["error"]
    no = dispatch.archive_project("tide-log")
    assert not no["ok"] and "git remote" in no["error"] and (roots / "tide-log").is_dir()
    (roots / "archive" / "tide-log").mkdir(parents=True)
    taken = dispatch.archive_project("tide-log", confirm_remote=True)
    assert not taken["ok"] and "not overwriting" in taken["error"] and (roots / "tide-log").is_dir()
    assert not dispatch.archive_project("ghost")["ok"]


def test_archive_project_with_a_confirmed_remote_moves_and_leaves_the_remote_alone(roots, monkeypatch):
    _doomed(roots, monkeypatch, remote=True)
    assert dispatch.archive_project("tide-log", confirm_remote=True)["ok"]
    out = dispatch.git(str(roots / "archive" / "tide-log"), "remote", "-v")
    assert "git@github.com:x/tide-log.git" in out


def test_unregister_hub_drops_the_entry_golden_and_workspace_folder_and_merges(roots, monkeypatch):
    hub = _doomed(roots, monkeypatch)
    (roots / "tide-log").rename(roots / "archive-tide")          # step 1 moved it
    res = dispatch.unregister_hub("tide-log")
    assert res["ok"] and res["merged"] and res["golden"] is True, res
    reg = dispatch.git(str(hub), "show", "master:projects.yaml")
    assert "tide-log" not in reg and "- id: nomad" in reg
    assert json.loads(dispatch.git(str(hub), "show", "master:nomad.code-workspace")) == {"folders": ["nomad"]}
    assert "autopilot/testdata/golden/tide-log-settings.json" not in dispatch.git(
        str(hub), "ls-tree", "-r", "--name-only", "master")
    assert "unregister/tide-log" not in dispatch.git(str(hub), "branch")
    assert not dispatch.git(str(hub), "status", "--short")
    assert dispatch.unregister_hub("tide-log")["note"] == "already unregistered"


def test_unregister_hub_refuses_hubs_and_leaves_a_dirty_master_alone(roots, monkeypatch):
    hub = _doomed(roots, monkeypatch)
    assert "hub repo" in dispatch.unregister_hub("nomad")["error"]
    assert not dispatch.unregister_hub("Bad Id")["ok"]
    with open(hub / "projects.yaml", "a") as f:
        f.write("  # someone's work in progress\n")
    res = dispatch.unregister_hub("tide-log")
    assert res["ok"] and not res["merged"] and res["branch"] == "unregister/tide-log", res
    assert (hub / "projects.yaml").read_text().endswith("# someone's work in progress\n")
    assert dispatch.unregister_hub("tide-log")["ok"]            # resumable from the branch


def test_drop_entry_keeps_comments_and_neighbours():
    text = ("projects:\n  # hub\n  - id: nomad\n    path: nomad\n    autopilot:\n      test: x\n\n"
            "  # group\n  - id: tide\n    path: tide\n    local_only: true\n"
            "  - id: tide-log\n    path: tide-log\n    autopilot:\n      test: \"y\"\n"
            "  - {id: flow, path: flow}\n  - {id: last, path: last}\n")
    out = dispatch._drop_entry(text, "tide-log")
    assert "tide-log" not in out and "  - id: tide\n    path: tide\n    local_only: true\n  - {id: flow" in out
    assert dispatch._drop_entry(text, "tide").count("id: tide") == 1          # not tide-log
    flow = dispatch._drop_entry(text, "flow")
    assert "flow" not in flow and "- {id: last" in flow and "# group" in flow
    assert dispatch._drop_entry(text, "absent") == text


def test_drop_board_row_handles_first_middle_last_and_keeps_the_format():
    rb = ('x = 1\nBOARDS = [\n  ["nomad-work", "NOMAD work"], ["civis", "Civis"],\n'
          '  ["nomad-portfolio", "NOMAD Portfolio"],\n  ["runematch", "RuneMatch"],\n  ["test", "Test"]\n'
          '].freeze\ny = 2\n')
    for gone in ("test", "runematch", "nomad-work", "civis", "nomad-portfolio"):
        out = dispatch._drop_board_row(rb, gone)
        m, idents = dispatch._boards(out)
        assert m and gone not in idents and out.startswith("x = 1\n") and out.endswith("].freeze\ny = 2\n")
        assert sorted(idents) == sorted(set(dispatch._boards(rb)[1]) - {gone}), gone
        assert ",\n]" not in out and ",," not in out
    assert dispatch._drop_board_row(rb, "absent") == rb


def test_archive_board_edits_seed_rb_on_a_branch_then_archives(tmp_path, monkeypatch):
    repo = _pm_repo(tmp_path, monkeypatch, tmp_path / "seeded")
    calls = []
    real = dispatch._run

    def fake(cmd, cwd, timeout=30):
        if cmd[0] == "docker":
            calls.append(cmd[:3])
            return 0, "[seed] x\n[archive] done\n" if cmd[1] == "exec" else ""
        return real(cmd, cwd, timeout)
    monkeypatch.setattr(dispatch, "_run", fake)
    res = dispatch.archive_board("runematch")
    assert res["ok"] and res["archived"] and res["merged"], res
    assert "runematch" not in (repo / "seed.rb").read_text() and "civis" in (repo / "seed.rb").read_text()
    assert "board-rm/runematch" not in dispatch.git(str(repo), "branch")
    assert calls == [["docker", "cp", calls[0][2]], ["docker", "exec", "nomad-openproject"]]
    again = dispatch.archive_board("runematch")             # row gone: only the archive step runs
    assert again["ok"] and again["branch"] == ""
    assert not dispatch.archive_board("nomad-work")["ok"] and not dispatch.archive_board("Bad Id")["ok"]
    assert "nomad-work" in (repo / "seed.rb").read_text()


def test_archive_board_says_so_when_openproject_does_not_archive(tmp_path, monkeypatch):
    _pm_repo(tmp_path, monkeypatch, tmp_path / "seeded")
    real = dispatch._run
    monkeypatch.setattr(dispatch, "_run", lambda cmd, cwd, timeout=30: (
        (1, "boom") if cmd[0] == "docker" and cmd[1] == "exec" else (0, "") if cmd[0] == "docker"
        else real(cmd, cwd, timeout)))
    res = dispatch.archive_board("runematch")
    assert not res["ok"] and "archive failed" in res["error"]


def test_archive_stream_runs_the_relay_helper_and_refuses_hubs(tmp_path, monkeypatch):
    relay = tmp_path / "relay"
    (relay / ".venv" / "bin").mkdir(parents=True)
    py = relay / ".venv" / "bin" / "python"
    py.write_text("#!/usr/bin/env bash\necho \"$@\" > seen\necho \"#$3: ok\"\n")
    py.chmod(0o755)
    env = tmp_path / "relay.env"
    env.write_text('NOMAD_ZULIP_OPERATOR_ID="8"\n')
    monkeypatch.setattr(dispatch, "RELAY", str(relay))
    monkeypatch.setattr(dispatch, "RELAY_ENV", str(env))
    assert dispatch.archive_stream("tide-log") == {"ok": True, "stream": "tide-log", "result": "ok"}
    assert (relay / "seen").read_text().strip() == "project_streams.py --archive tide-log"
    assert not dispatch.archive_stream("nomad")["ok"] and not dispatch.archive_stream("Bad Id")["ok"]
    py.write_text("#!/usr/bin/env bash\necho \"#$3: error: Must be an organization administrator\"\nexit 1\n")
    res = dispatch.archive_stream("tide-log")
    assert not res["ok"] and "administrator" in res["result"]


def test_website_template_adds_the_claude_allowlist(roots):
    _mark(roots, "nomad", "NOMAD")
    _registry(roots, "  - {id: nomad, path: nomad}\n")
    res = dispatch.new_project("Site X", template="website")
    assert res["ok"], res
    cfg = json.loads((roots / "site-x" / ".claude" / "settings.json").read_text())
    allow = cfg["permissions"]["allow"]
    assert "Bash(npm run *)" in allow
    assert not any("push" in a or "rm " in a for a in allow)
    assert "WEBSITES.md" in (roots / "site-x" / "AGENTS.md").read_text()
    assert (roots / "site-x" / "docs" / "WEBSITE-CHECKLIST.md").is_file()
    plain = dispatch.new_project("Plain")
    assert not (roots / "plain" / ".claude").exists() and plain["ok"]


# ── #1373: the project type at registration ─────────────────────────────────────────────

def test_an_untyped_entry_is_what_it_was_before_types():
    assert dispatch._registry_entry("x1", "x1", False, True) == dispatch._registry_entry(
        "x1", "x1", False, True, "generic", (), {"pre_merge": (), "milestone": ()})
    assert "type:" not in dispatch._registry_entry("x1", "x1", False, True, "generic")


def test_a_typed_entry_carries_the_type_its_policies_and_the_profiles_gates():
    prof, pols, why = dispatch._type_and_policies("website", ())
    assert not why and pols == ()
    e = dispatch._registry_entry("site1", "site1", False, True, prof.id, ("engine-unity",), prof.gates)
    assert "    type: website\n" in e and "    policies: [engine-unity]\n" in e
    assert '      pre_merge_gate: ["npm ci", "npm run build"]' in e
    no_ap = dispatch._registry_entry("site1", "site1", False, False, prof.id, (), prof.gates)
    assert "gate" not in no_ap                     # gates live in the autopilot block only


@pytest.mark.parametrize("ptype,pols,want", [
    ("spaceship", (), "spaceship"), ("Website", (), "not a project type"),
    ("game", ("no-such-policy",), "no policy 'no-such-policy'"),
    ("game", ("../guardrail-guard-hook",), "no policy"),
])
def test_an_unknown_type_or_policy_is_refused(ptype, pols, want):
    _, _, why = dispatch._type_and_policies(ptype, pols)
    assert want in why


def test_register_refuses_a_bad_type_before_touching_the_hub():
    res = dispatch.register_hub("nope-x", ptype="spaceship")
    assert res["ok"] is False and "spaceship" in res["error"]


def test_the_profiles_payload_lists_generic_last():
    got = dispatch.profiles_payload()
    assert got["ok"] and got["profiles"][-1]["id"] == "generic"
    web = next(p for p in got["profiles"] if p["id"] == "website")
    assert web["gates"]["pre_merge"] == ["npm ci", "npm run build"]
    assert json.loads(json.dumps(got)) == got           # it goes over HTTP as is
