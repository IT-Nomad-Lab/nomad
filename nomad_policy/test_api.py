"""Tests for nomad_policy/api.py (#1396): read, save to a branch, guardrail refusal, attach and
detach, effective access, simulator (deny beats allow, A8) and the tag dry run. Writes go to a
throwaway clone of this repo, never to the real one.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest nomad_policy -q
"""
import os
import subprocess
import sys

import pytest
import yaml

from autopilot import projects
from nomad_policy import api, schema

NOMAD = schema.NOMAD


@pytest.fixture(scope="module")
def reg():
    if not os.path.isdir(os.path.expanduser(projects.HUB_BASE)):
        pytest.skip("no hub checkout")
    return schema.Registry.live()


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "nomad"
    subprocess.run(["git", "clone", "-q", "--shared", "-b", api.MASTER, NOMAD, str(root)], check=True)
    return str(root)


def _git(root, *a):
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True).stdout


def _master_untouched(root):
    assert _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() == api.MASTER
    assert _git(root, "status", "--porcelain") == ""
    assert "nomad-policy-" not in _git(root, "worktree", "list")


POLICY = """id: test-extra
role: permission
description: A test policy.
statements:
  - sid: extra-figma
    effect: allow
    kind: mcp
    match: [figma]
"""


# ---- read --------------------------------------------------------------------------------------

def test_list_marks_guardrails_and_attachments(reg):
    got = {p["id"]: p for p in api.list_policies(registry=reg)["policies"]}
    assert got["guardrail-guard-hook"]["guardrail"] and got["guardrail-guard-hook"]["role"] == "guardrail"
    assert got["autopilot-worker-base"]["guardrail"]              # in policies/ itself: every project gets it
    assert not got["website-default"]["guardrail"]
    assert "website" in got["website-default"]["attached"]["types"]
    assert got["engine-unity"]["attached"] == {"types": [], "projects": []}   # an example, not attached yet


def test_get_returns_the_file_and_404s_an_unknown_id(reg):
    got = api.get_policy("design-read", registry=reg)
    assert got["file"] == "policies/types/design-read.yaml" and got["policy"]["id"] == "design-read"
    with pytest.raises(api.ApiError) as e:
        api.get_policy("nope", registry=reg)
    assert e.value.status == 404


def test_list_gives_the_editor_its_targets_and_choices(reg):
    got = api.list_policies(registry=reg)
    assert "website" in got["types"] and "nomad" in got["projects"]
    assert got["schema"]["effects"] == list(schema.EFFECTS) and got["schema"]["kinds"] == list(schema.KINDS)
    assert set(got["schema"]) == {"roles", "effects", "kinds", "when_keys", "setting_keys", "channels", "tiers"}


def test_validate_parses_without_writing(reg, repo):
    got = api.validate("test-extra", POLICY, reg)
    assert got["ok"] and got["role"] == "permission" and got["policy"]["statements"][0]["sid"] == "extra-figma"
    with pytest.raises(api.ApiError) as e:
        api.validate("test-extra", POLICY.replace("figma", "no-such-server"), reg)
    assert e.value.status == 400 and "no-such-server" in str(e.value)
    with pytest.raises(api.ApiError) as e:
        api.validate("legacy-x", "id: legacy-x\nrole: permission\n", reg)
    assert e.value.status == 403
    assert "policy/test-extra" not in _git(repo, "branch")


def test_get_shows_the_unmerged_edit_so_a_second_save_keeps_the_first(reg, repo):
    assert api.get_policy("design-read", repo, reg)["pending"] is None
    text = api.get_policy("design-read", repo, reg)["yaml"] + "# edited in the cockpit\n"
    api.save("design-read", text, repo, reg)
    got = api.get_policy("design-read", repo, reg)
    assert got["pending"] == {"branch": "policy/design-read", "ahead": 1, "yaml": text}
    assert got["yaml"] != text                                      # master's file is unchanged


# ---- save --------------------------------------------------------------------------------------

def test_save_writes_a_new_policy_on_a_branch_not_master(reg, repo):
    got = api.save("test-extra", POLICY, repo, reg)
    assert got["branch"] == "policy/test-extra" and got["file"] == "policies/types/test-extra.yaml"
    assert "+  - sid: extra-figma" in got["diff"]
    assert _git(repo, "show", "policy/test-extra:policies/types/test-extra.yaml") == POLICY
    assert not os.path.exists(os.path.join(repo, "policies/types/test-extra.yaml"))
    _master_untouched(repo)


def test_a_second_save_adds_a_commit_to_the_same_branch(reg, repo):
    api.save("test-extra", POLICY, repo, reg)
    got = api.save("test-extra", POLICY.replace("A test policy.", "Edited."), repo, reg)
    assert _git(repo, "rev-list", "--count", f"{api.MASTER}..policy/test-extra").strip() == "2"
    assert "+description: Edited." in got["diff"]


def test_an_unchanged_save_makes_no_commit(reg, repo):
    text = api.get_policy("design-read", repo, reg)["yaml"]
    got = api.save("design-read", text, repo, reg)
    assert got["diff"] == "" and _git(repo, "rev-list", "--count", f"{api.MASTER}..policy/design-read").strip() == "0"


@pytest.mark.parametrize("text, msg", [
    ("id: test-extra\nrole: permission\nbogus: 1\n", "unknown"),
    ("id: other\nrole: permission\n", "other"),
    ("id: test-extra\nrole: permission\nstatements:\n  - {effect: allow, kind: mcp, match: [no-such-server]}\n", "no-such-server"),
    ("id: [\n", "invalid YAML"),
    ("", "yaml must be"),
])
def test_save_refuses_an_invalid_policy(reg, repo, text, msg):
    with pytest.raises(api.ApiError, match=msg) as e:
        api.save("test-extra", text, repo, reg)
    assert e.value.status == 400
    _master_untouched(repo)
    assert "policy/test-extra" not in _git(repo, "branch")


def test_legacy_policies_belong_to_the_tag(reg, repo):
    with pytest.raises(api.ApiError, match="tag operation") as e:
        api.save("legacy-nomad", "id: legacy-nomad\nrole: permission\n", repo, reg)
    assert e.value.status == 403


# ---- guardrails (A8: an agent cannot write a guardrail) ----------------------------------------

@pytest.mark.parametrize("pid", ["guardrail-guard-hook", "guardrail-autopilot-worker", "autopilot-worker-base"])
def test_the_ordinary_save_refuses_a_guardrail(reg, repo, pid):
    text = api.get_policy(pid, repo, reg)["yaml"]
    with pytest.raises(api.ApiError, match="guardrail") as e:
        api.save(pid, text, repo, reg)
    assert e.value.status == 403
    assert f"policy/{pid}" not in _git(repo, "branch")


def test_a_new_policy_with_role_guardrail_is_refused_too(reg, repo):
    with pytest.raises(api.ApiError, match="guardrail") as e:
        api.save("guardrail-new", "id: guardrail-new\nrole: guardrail\n", repo, reg)
    assert e.value.status == 403


def test_a_guardrail_save_needs_the_typed_confirmation(reg, repo):
    pid = "guardrail-guard-hook"
    text = api.get_policy(pid, repo, reg)["yaml"]
    for bad in (None, "", "yes", "guardrail-autopilot-worker"):
        with pytest.raises(api.ApiError, match="confirm") as e:
            api.save_guardrail(pid, text, bad, repo, reg)
        assert e.value.status == 403
    got = api.save_guardrail(pid, text.replace("Layer 1", "Layer one", 1), pid, repo, reg)
    assert got["guardrail"] and got["branch"] == f"policy/{pid}" and "Layer one" in got["diff"]
    _master_untouched(repo)


def test_the_guardrail_route_refuses_an_ordinary_policy(reg, repo):
    with pytest.raises(api.ApiError, match="not a guardrail"):
        api.save_guardrail("test-extra", POLICY, "test-extra", repo, reg)


# ---- attach and detach -------------------------------------------------------------------------

def test_attach_to_a_type_then_detach(reg, repo):
    got = api.attach("type", "service", "design-read", root=repo, registry=reg)
    assert got["branch"] == "policy/attach-type-service" and got["policies"][-1] == "design-read"
    text = _git(repo, "show", "policy/attach-type-service:profiles/service.yaml")
    assert yaml.safe_load(text)["policies"] == got["policies"]
    _master_untouched(repo)
    got = api.attach("type", "website", "design-read", detach=True, root=repo, registry=reg)
    assert "design-read" not in got["policies"] and "-policies: [website-default, design-read" in got["diff"]


def test_attach_to_a_project_adds_and_removes_its_policies_line(reg, repo):
    got = api.attach("project", "nomad-crm", "design-read", root=repo, registry=reg)
    entry = next(e for e in yaml.safe_load(_git(repo, "show", f"{got['branch']}:projects.yaml"))["projects"]
                 if e["id"] == "nomad-crm")
    assert entry["policies"] == ["design-read"]
    got = api.attach("project", "nomad-crm", "design-read", detach=True, root=repo, registry=reg)
    after = _git(repo, "show", f"{got['branch']}:projects.yaml")
    entry = next(e for e in yaml.safe_load(after)["projects"] if e["id"] == "nomad-crm")
    assert "policies" not in entry
    assert after == _git(repo, "show", f"{api.MASTER}:projects.yaml")   # back to the bytes it had
    _master_untouched(repo)


@pytest.mark.parametrize("args, status", [
    (("type", "website", "guardrail-guard-hook"), 403),
    (("type", "website", "autopilot-worker-base"), 403),
    (("type", "website", "design-read"), 409),           # already there
    (("type", "nope", "design-read"), 404),
    (("project", "nope", "design-read"), 404),
    (("type", "website", "nope"), 404),
    (("planet", "website", "design-read"), 400),
])
def test_attach_refusals(reg, repo, args, status):
    with pytest.raises(api.ApiError) as e:
        api.attach(*args, root=repo, registry=reg)
    assert e.value.status == status


def test_detach_what_is_not_attached_is_refused(reg, repo):
    with pytest.raises(api.ApiError) as e:
        api.attach("type", "generic", "design-read", detach=True, root=repo, registry=reg)
    assert e.value.status == 409


# ---- effective access and the simulator --------------------------------------------------------

def test_effective_access_names_the_deciding_statement(reg):
    got = api.effective("crypto-trading-bot", registry=reg)
    gw = {i["name"]: i for i in got["items"] if i["kind"] == "mcp" and i["channel"] == "gateway"}
    assert gw["alpaca"]["effect"] == "allow" and gw["alpaca"]["policy"] == "legacy-crypto-trading-bot"
    assert gw["figma"]["effect"] == "deny" and gw["figma"]["reason"] == "default-deny"
    assert got["layers"]["guardrail"] == ["guardrail-guard-hook", "guardrail-autopilot-worker"]
    assert "cap" in got["settings"] or got["settings"] == {} or isinstance(got["settings"], dict)


def test_the_simulator_says_deny_beats_allow(reg):
    got = api.simulate("nomad", "tool", "Bash(git push *)", registry=reg)
    assert got["effect"] == "deny" and got["reason"] == "explicit-deny" and got["layer"] == "guardrail"


def test_the_simulator_reports_an_allow_and_its_statement(reg):
    got = api.simulate("crypto-trading-bot", "mcp", "alpaca", {"channel": "gateway"}, registry=reg)
    assert got["effect"] == "allow" and got["sid"] == "legacy-mcp-gateway"


@pytest.mark.parametrize("kind, name, ctx", [("planet", "x", None), ("mcp", "", None), ("mcp", "x", {"colour": "red"})])
def test_the_simulator_refuses_a_bad_request(reg, kind, name, ctx):
    with pytest.raises(api.ApiError):
        api.simulate("nomad", kind, name, ctx, registry=reg)


def test_an_unknown_project_is_a_404(reg):
    with pytest.raises(api.ApiError) as e:
        api.effective("nope", registry=reg)
    assert e.value.status == 404


# ---- tag dry run -------------------------------------------------------------------------------

def test_the_tag_dry_run_shows_an_empty_diff_and_the_proposals(reg):
    got = api.tag_dry_run("crypto-trading-bot", "website", registry=reg)
    assert got["can_save"] and got["diff"] == []
    assert "mcp:gateway:+figma" in got["proposed"]


def test_the_tag_dry_run_refuses_an_unknown_type(reg):
    with pytest.raises(api.ApiError, match="profile"):
        api.tag_dry_run("crypto-trading-bot", "spaceship", registry=reg)


# ---- tag save and accept (#1398) ---------------------------------------------------------------

def _compile_check(repo, ref):
    """policy-compile.py --check run inside a worktree of `ref`: what the branch would deploy."""
    wt = os.path.join(repo, "..", "check-" + ref.replace("/", "-"))
    subprocess.run(["git", "worktree", "add", "-q", wt, ref], cwd=repo, check=True)
    try:
        return subprocess.run([sys.executable, "scripts/policy-compile.py", "--check"], cwd=wt,
                              capture_output=True, text=True, timeout=300)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=repo)


def test_tagging_a_real_project_end_to_end_on_a_copy_keeps_its_configuration(reg, repo):
    """The #1398 gate: the tag is saved on a branch, and that branch compiles to the baseline."""
    got = api.tag_save("crypto-trading-bot", "website", repo, reg)
    assert got["branch"] == "policy/tag-crypto-trading-bot"
    assert "mcp:gateway:+figma" in got["held"]
    assert set(got["files"]) == {"policies/legacy/legacy-crypto-trading-bot.yaml", "projects.yaml"}
    assert "+    type: website" in got["diff"]
    _master_untouched(repo)
    r = _compile_check(repo, got["branch"])
    assert r.returncode == 0 and "compile identically to the baseline" in r.stdout, r.stdout + r.stderr
    state = api.tag_state("crypto-trading-bot", repo)
    assert state["branch"] == got["branch"] and state["type"] == "website" and state["held"] == got["held"]


def test_accept_lifts_one_hold_and_that_change_shows(reg, repo):
    api.tag_save("crypto-trading-bot", "website", repo, reg)
    got = api.tag_accept("crypto-trading-bot", "mcp:gateway:+figma", repo)
    assert "mcp:gateway:+figma" not in got["held"]
    assert api.tag_state("crypto-trading-bot", repo)["held"] == got["held"]
    r = _compile_check(repo, "policy/tag-crypto-trading-bot")
    assert r.returncode != 0 and "crypto-trading-bot" in r.stdout + r.stderr   # the accepted add is a real change
    with pytest.raises(api.ApiError) as e:
        api.tag_accept("crypto-trading-bot", "mcp:gateway:+figma", repo)      # already lifted
    assert e.value.status == 409


def test_tag_save_refusals(reg, repo):
    with pytest.raises(api.ApiError) as e:
        api.tag_accept("crypto-trading-bot", "mcp:gateway:+figma", repo)      # nothing saved yet
    assert e.value.status == 409
    api.tag_save("crypto-trading-bot", "website", repo, reg)
    with pytest.raises(api.ApiError) as e:
        api.tag_save("crypto-trading-bot", "website", repo, reg)              # would drop accepted changes
    assert e.value.status == 409 and "already has a saved tag" in str(e.value)
    with pytest.raises(api.ApiError) as e:
        api.tag_save("no-such-project", "website", repo, reg)
    assert e.value.status == 404
    subprocess.run(["git", "checkout", "-q", "-b", "elsewhere"], cwd=repo, check=True)
    with pytest.raises(api.ApiError) as e:
        api.tag_save("nomad-crm", "service", repo, reg)
    assert e.value.status == 409 and "elsewhere" in str(e.value)


def test_validate_shows_the_diff_against_master(reg, repo):
    text = api.get_policy("design-read", repo, reg)["yaml"]
    assert api.validate("design-read", text, reg, repo)["diff"] == ""
    got = api.validate("design-read", text + "# note\n", reg, repo)
    assert got["file"] == "policies/types/design-read.yaml" and "+# note" in got["diff"]
