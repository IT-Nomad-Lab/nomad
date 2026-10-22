"""Tests for nomad_projects/profile_api.py (#1377, #1379): the cockpit's profile API. List, get,
validate (errors and the diff against master), the dry run of what a new project of a type gets,
for every profile and for an edit, and save on branch profile/<id>. Saves go to a throwaway clone
of this repo, never to the real one.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest nomad_projects -q
"""
import os
import subprocess

import pytest
import yaml

from autopilot import projects
from nomad_policy import api, schema
from nomad_projects import profile_api as pa

NOMAD = schema.NOMAD


@pytest.fixture(scope="module")
def reg():
    if not os.path.isdir(os.path.expanduser(projects.HUB_BASE)):
        pytest.skip("no hub checkout")
    return schema.Registry.live()


def _ids():
    return sorted(f[:-5] for f in os.listdir(os.path.join(NOMAD, "profiles")) if f.endswith(".yaml"))


def _text(pid):
    with open(os.path.join(NOMAD, "profiles", pid + ".yaml")) as f:
        return f.read()


def test_list_has_every_profile_and_the_editor_choices(reg):
    got = pa.list_profiles(registry=reg)
    assert got["ok"] and got["default"] == "generic"
    assert [p["id"] for p in got["profiles"]] == _ids()
    assert all(p["error"] is None and p["title"] for p in got["profiles"])
    c = got["choices"]
    names = [p["id"] for p in c["policies"]]
    assert "design-read" in names
    assert not any(n.startswith("legacy-") or n.startswith("guardrail-") for n in names)
    assert set(c["models"]) == {"sonnet", "opus", "haiku", "fable"}
    assert "dev" in c["lanes"] and c["roles"] and "id" in c["required"]


def test_get_returns_the_text_and_404s_an_unknown_id(reg):
    got = pa.get_profile("website", registry=reg)
    assert got["ok"] and got["file"] == "profiles/website.yaml" and got["error"] is None
    assert got["yaml"] == _text("website") and got["profile"]["id"] == "website"
    with pytest.raises(api.ApiError) as e:
        pa.get_profile("no-such-type", registry=reg)
    assert e.value.status == 404
    with pytest.raises(api.ApiError) as e:
        pa.get_profile("../etc", registry=reg)
    assert e.value.status == 400


def test_validate_an_unchanged_profile_has_an_empty_diff(reg):
    got = pa.validate("website", _text("website"), registry=reg)
    assert got["ok"] and not got["new"] and got["diff"] == ""


def test_validate_shows_the_diff_of_an_edit(reg):
    raw = yaml.safe_load(_text("website"))
    raw["description"] = "An edited description."
    got = pa.validate("website", yaml.safe_dump(raw, sort_keys=False), registry=reg)
    assert got["diff"].startswith("--- a/profiles/website.yaml")
    assert "+description: An edited description." in got["diff"].splitlines()


@pytest.mark.parametrize("text, msg", [
    ("id: website\n", "missing required field"),
    ("id: website\ntitle: T\ndescription: D\nmcps: [figma]\n", "no longer a profile field"),
    ("id: website\ntitle: T\ndescription: D\npolicies: [no-such-policy]\n", "unknown policy"),
    ("id: website\ntitle: T\ndescription: D\npolicies: [guardrail-guard-hook]\n", "guardrail"),
    ("id: other\ntitle: T\ndescription: D\n", "must equal the file name"),
    ("id: website\ntitle: T\ndescription: D\npolicies: [autopilot-worker-base]\n", "not a type policy"),
    ("id: [\n", "YAML"),
])
def test_validate_refuses_a_bad_profile(reg, text, msg):
    with pytest.raises(api.ApiError) as e:
        pa.validate("website", text, registry=reg)
    assert e.value.status == 400 and msg in str(e.value)


@pytest.mark.parametrize("pid", _ids())
def test_every_profile_dry_runs(reg, pid):
    got = pa.dry_run(pid, registry=reg)
    assert got["ok"] and not got["edited"] and got["changes"] is None
    g = got["gets"]
    for k in ("interview", "registry_entry", "stack", "agents", "gates", "files", "autopilot",
              "policies", "layers", "access"):
        assert k in g
    entry = yaml.safe_load(g["registry_entry"])[0]
    assert entry.get("type", "generic") == pid
    assert got["facts"] and all(isinstance(f, str) for f in got["facts"])


def test_an_unchanged_edit_changes_nothing(reg):
    assert pa.dry_run("website", _text("website"), registry=reg)["changes"] == ""


def test_dropping_a_policy_shows_the_lost_access(reg):
    raw = yaml.safe_load(_text("website"))
    assert "design-read" in raw["policies"]
    raw["policies"] = [p for p in raw["policies"] if p != "design-read"]
    got = pa.dry_run("website", yaml.safe_dump(raw, sort_keys=False), registry=reg)
    lines = got["changes"].splitlines()
    assert "-policy design-read" in lines
    assert any(ln.startswith("-mcp ") and "figma" in ln for ln in lines)
    assert not any(ln.startswith("+policy") for ln in lines)


def test_a_new_type_dry_runs_against_nothing(reg):
    text = "id: brand-new\ntitle: New\ndescription: A type not on master.\npolicies: [design-read]\n"
    got = pa.dry_run("brand-new", text, registry=reg)
    assert got["new"] and "+policy design-read" in got["changes"].splitlines()
    with pytest.raises(api.ApiError) as e:
        pa.dry_run("brand-new", registry=reg)
    assert e.value.status == 404


# ---- save (#1379): on profile/<id>, never master ---------------------------------------------

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


def _edited(pid, desc="An edited description."):
    raw = yaml.safe_load(_text(pid))
    raw["description"] = desc
    return yaml.safe_dump(raw, sort_keys=False)


NEW = "id: brand-new\ntitle: Brand new\ndescription: A type saved from the cockpit.\npolicies: [design-read]\n"


def test_save_writes_on_the_branch_and_never_master(reg, repo):
    master = _git(repo, "rev-parse", api.MASTER)
    got = pa.save("website", _edited("website"), repo, reg)
    assert got["ok"] and got["branch"] == "profile/website" and not got["new"] and not got["unchanged"]
    assert "+description: An edited description." in got["diff"].splitlines()
    assert _git(repo, "show", "profile/website:profiles/website.yaml") == _edited("website")
    assert _git(repo, "rev-parse", api.MASTER) == master
    with open(os.path.join(repo, "profiles", "website.yaml")) as f:
        assert f.read() == _text("website")
    _master_untouched(repo)


def test_the_saved_edit_shows_as_pending_and_a_second_save_adds_a_commit(reg, repo):
    pa.save("website", _edited("website"), repo, reg)
    got = pa.get_profile("website", repo, reg)
    assert got["pending"] == {"branch": "profile/website", "ahead": 1, "yaml": _edited("website")}
    assert got["yaml"] == _text("website") and not got["new"]
    row = {p["id"]: p for p in pa.list_profiles(repo, reg)["profiles"]}["website"]
    assert row["pending"] and not row["new"]
    got = pa.save("website", _edited("website", "Edited twice."), repo, reg)
    assert _git(repo, "rev-list", "--count", f"{api.MASTER}..profile/website").strip() == "2"
    assert "+description: Edited twice." in got["diff"].splitlines()      # the diff is against master


def test_an_unchanged_save_writes_nothing(reg, repo):
    got = pa.save("website", _text("website"), repo, reg)
    assert got["unchanged"] and got["branch"] is None and got["diff"] == ""
    assert "profile/website" not in _git(repo, "branch")
    pa.save("website", _edited("website"), repo, reg)
    got = pa.save("website", _edited("website"), repo, reg)              # equal to the branch
    assert got["unchanged"] and got["branch"] == "profile/website" and got["diff"]
    assert _git(repo, "rev-list", "--count", f"{api.MASTER}..profile/website").strip() == "1"


def test_a_new_type_lives_on_its_branch_until_merged(reg, repo):
    got = pa.save("brand-new", NEW, repo, reg)
    assert got["new"] and got["branch"] == "profile/brand-new" and "+description: A type saved from the cockpit." in got["diff"].splitlines()
    assert not os.path.exists(os.path.join(repo, "profiles", "brand-new.yaml"))
    row = {p["id"]: p for p in pa.list_profiles(repo, reg)["profiles"]}["brand-new"]
    assert row["new"] and row["pending"] and row["error"] is None and row["policies"] == ["design-read"]
    got = pa.get_profile("brand-new", repo, reg)
    assert got["new"] and got["yaml"] is None and got["pending"]["yaml"] == NEW
    assert got["profile"]["title"] == "Brand new" and got["error"] is None
    _master_untouched(repo)


@pytest.mark.parametrize("pid, text, msg", [
    ("website", "id: website\n", "missing required field"),
    ("website", "id: website\ntitle: T\ndescription: D\nmcps: [figma]\n", "mcps"),
    ("website", "id: website\ntitle: T\ndescription: D\npolicies: [guardrail-guard-hook]\n", "policies[0]"),
    ("website", "id: website\ntitle: T\ndescription: D\npolicies: [autopilot-worker-base]\n", "policies[0]"),
    ("website", "id: other\ntitle: T\ndescription: D\n", "must equal the file name"),
    ("website", "id: [\n", "invalid YAML"),
    ("website", "", "yaml must be"),
    ("../etc", "id: x\n", "not a profile id"),
])
def test_save_refuses_a_bad_profile_with_the_field_named(reg, repo, pid, text, msg):
    with pytest.raises(api.ApiError) as e:
        pa.save(pid, text, repo, reg)
    assert e.value.status == 400 and msg in str(e.value)
    assert "profile/" not in _git(repo, "branch")
    _master_untouched(repo)
