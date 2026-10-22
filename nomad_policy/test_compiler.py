"""Tests for nomad_policy/compiler.py (#1392): one golden test per enforcement point, the
baseline round trip on a fixture project, and the round trip on every committed baseline.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest nomad_policy -q
"""
import dataclasses
import glob
import json
import os

import pytest

from autopilot import permissions, projects
from nomad_policy import compiler
from nomad_policy import evaluator as ev
from nomad_policy import schema
from nomad_policy.legacy import legacy_policy

NOMAD = schema.NOMAD
snap = compiler.snapshot_module()
REG = schema.Registry(frozenset({"alpaca", "figma", "nomad-pm", "nomad-image", "supabase"}), frozenset())
UNI = compiler.Universe(gateway=frozenset({"alpaca", "figma"}),
                        local=frozenset({"nomad-pm", "nomad-image", "figma"}),
                        repo=frozenset(REG.mcps))


def _file(pid):
    return schema.load_file(os.path.join(schema.POLICIES_DIR, pid + ".yaml"), schema.Registry())


GUARD = tuple(_file(p) for p in compiler.GUARDRAILS)
BASE = _file(compiler.WORKER_BASE)


def pol(pid, statements=(), **kw):
    return schema.validate({"id": pid, "role": "permission", "statements": list(statements), **kw}, REG)


def allow(*names, **when):
    return {"effect": "allow", "kind": "mcp", "match": list(names), **({"when": when} if when else {})}


def lay(type=(), project=()):
    return ev.Layers(GUARD, (BASE,) + tuple(type), tuple(project))


OBSERVED = {"gateway": {"key_present": True, "servers": []}, "local_model_set": [], "repo_mcp_json": []}


# ---- MCP: gateway grants, local-model set, repo .mcp.json --------------------------------------

def test_gateway_grants_are_the_gateway_servers_the_policy_allows():
    mcp, gaps = compiler.compile_mcp(lay(project=[pol("p", [allow("figma", "nomad-pm", channel="gateway")])]),
                                     UNI, OBSERVED)
    assert mcp["gateway"] == {"key_present": True, "servers": ["figma"]}   # nomad-pm is not a gateway server
    assert mcp["local_model_set"] == [] and mcp["repo_mcp_json"] == []
    assert not [g for g in gaps if g.startswith("gateway")]


def test_a_gateway_grant_without_a_key_is_a_gap_not_a_grant():
    obs = {**OBSERVED, "gateway": {"key_present": False, "servers": []}}
    mcp, gaps = compiler.compile_mcp(lay(project=[pol("p", [allow("alpaca", channel="gateway")])]), UNI, obs)
    assert mcp["gateway"]["servers"] == []
    assert any("no gateway key" in g for g in gaps)


def test_local_model_set_is_the_local_servers_the_policy_allows():
    mcp, _ = compiler.compile_mcp(lay(project=[pol("p", [allow("nomad-*", channel="local")])]), UNI, OBSERVED)
    assert mcp["local_model_set"] == ["nomad-image", "nomad-pm"]
    assert mcp["gateway"]["servers"] == [] and mcp["repo_mcp_json"] == []


def test_an_allow_without_a_channel_reaches_every_point_that_serves_the_name():
    mcp, _ = compiler.compile_mcp(lay(type=[pol("t", [allow("figma")])]), UNI, OBSERVED)
    assert (mcp["gateway"]["servers"], mcp["local_model_set"], mcp["repo_mcp_json"]) == (["figma"],) * 3


def test_repo_mcp_json_keeps_its_gateway_entry_and_a_deny_wins():
    obs = {**OBSERVED, "repo_mcp_json": ["mcp-gateway", "supabase"]}
    deny = schema.validate({"id": "g", "role": "guardrail", "statements": [
        {"effect": "deny", "kind": "mcp", "match": ["supabase"]}]}, REG)
    L = ev.Layers(GUARD + (deny,), (BASE,), (pol("p", [allow("supabase", "figma", channel="repo")]),))
    mcp, _ = compiler.compile_mcp(L, UNI, obs)
    assert mcp["repo_mcp_json"] == ["figma", "mcp-gateway"]


def test_an_mcp_ask_is_left_out_and_reported():
    p = pol("p", [allow("figma", channel="local"), {"effect": "ask", "kind": "mcp", "match": ["figma"]}])
    mcp, gaps = compiler.compile_mcp(lay(project=[p]), UNI, OBSERVED)
    assert mcp["local_model_set"] == []
    assert any("need an ask" in g and "figma" in g for g in gaps)


def test_missing_sources_stay_missing_when_nothing_is_allowed():
    obs = {"gateway": {"key_present": False, "servers": []}, "local_model_set": None, "repo_mcp_json": None}
    mcp, _ = compiler.compile_mcp(lay(), UNI, obs)
    assert mcp == obs


# ---- autopilot settings ------------------------------------------------------------------------

AP = projects.Autopilot(test={"default": projects.Command("make test")},
                        pre_merge_gate=(projects.Command("make test"),),
                        milestone_gate=(projects.Command("make test"),), cap=3, model="sonnet")


def test_policy_settings_replace_the_autopilot_keys_and_the_most_specific_layer_wins():
    t = pol("t", settings={"cap": 2, "model": "opus"})
    p = pol("p", settings={"cap": 4, "pre_merge_gate": ["make lint", {"run": "make e2e", "env": {"A": "1"}, "locks": ["/tmp/l"]},
                                                        "test:default"],
                           "deny_edit_globs": ["secrets/**"]})
    got = compiler.compile_autopilot(lay(type=[t], project=[p]), AP)
    assert (got.cap, got.model, got.deny_edit_globs) == (4, "opus", ("secrets/**",))
    assert got.pre_merge_gate == (projects.Command("make lint"), projects.Command("make e2e", ("/tmp/l",), {"A": "1"}),
                                  AP.test["default"])
    assert got.milestone_gate == AP.milestone_gate            # no policy sets it: projects.yaml stands
    assert got.test == AP.test


def test_no_autopilot_block_compiles_to_none():
    assert compiler.compile_autopilot(lay(type=[pol("t", settings={"cap": 2})]), None) is None


# ---- autopilot permission template -------------------------------------------------------------

def test_the_layer_1_policies_compile_to_todays_template():
    with open(permissions.TEMPLATE) as f:
        want = json.load(f)["permissions"]
    assert compiler.compile_template(lay()) == {"permissions": want}


def test_a_project_tool_deny_and_ask_join_the_template_and_a_guardrail_allow_cannot():
    p = pol("p", [{"effect": "deny", "kind": "tool", "match": ["Bash(make deploy *)"]},
                  {"effect": "ask", "kind": "tool", "match": ["Bash(make release *)"]},
                  {"effect": "allow", "kind": "tool", "match": ["Bash(make *)"]}])
    t = compiler.compile_template(lay(project=[p]))["permissions"]
    assert t["deny"][-1] == "Bash(make deploy *)" and t["ask"] == ["Bash(make release *)"]
    assert t["allow"][-1] == "Bash(make *)"
    assert t["defaultMode"] == "dontAsk"


# ---- the round trip: snapshot -> legacy policy -> compile == snapshot --------------------------

REGISTRY = """\
projects:
  - id: alpha
    path: alpha
    autopilot:
      test: "echo test {filter}"
      cap: 2
      model: sonnet
      deny_edit_globs: ["secrets/**"]
  - id: beta
    path: beta
"""


@pytest.fixture
def fixture_project(tmp_path):
    for d in ("alpha", "beta"):
        (tmp_path / d).mkdir()
    (tmp_path / "alpha" / ".mcp.json").write_text(json.dumps({"mcpServers": {"supabase": {}, "mcp-gateway": {}}}))
    reg = tmp_path / "projects.yaml"
    reg.write_text(REGISTRY)
    src = snap.Sources(gateway=lambda p: (p == "alpha", ["figma"] if p == "alpha" else []),
                       grants=lambda: {"default": ["nomad-pm"], "projects": {"alpha": ["nomad-image"]}},
                       state_dir=str(tmp_path / "state"), default_branch="main")
    baselines = {k: json.loads(snap.render(v)) for k, v in snap.snapshot(str(reg), src, base=str(tmp_path)).items()}
    return projects.load(str(reg), base=str(tmp_path)), baselines, src


def _compile(p, base, src, extra=()):
    L = ev.Layers(GUARD, (BASE,), (schema.validate(legacy_policy(base), REG),) + tuple(extra))
    return compiler.compile_project(p, L, UNI, base["mcp"], src)


def test_round_trip_on_a_fixture_project_is_byte_identical(fixture_project):
    ps, baselines, src = fixture_project
    for p in ps:
        got, gaps = _compile(p, baselines[p.id], src)
        assert compiler.render(got) == compiler.render(compiler.baseline_fields(baselines[p.id])), p.id
        assert not any("agent-bridge" in g for g in gaps)


def test_the_check_bites_when_a_policy_changes_the_output(fixture_project):
    ps, baselines, src = fixture_project
    alpha = next(p for p in ps if p.id == "alpha")
    extra = pol("p2", [allow("alpaca", channel="gateway"),
                       {"effect": "deny", "kind": "tool", "match": ["Bash(make deploy *)"]}], settings={"cap": 5})
    got, _ = _compile(alpha, baselines["alpha"], src, [extra])
    want = compiler.baseline_fields(baselines["alpha"])
    assert got["mcp"]["gateway"]["servers"] == ["alpaca", "figma"] != want["mcp"]["gateway"]["servers"]
    assert got["autopilot"]["cap"] == 5
    assert "Bash(make deploy *)" in got["autopilot_permissions"]["settings"]["permissions"]["deny"]


def test_a_policy_deny_on_the_projects_own_test_command_is_a_refusal(fixture_project):
    """alpha's test command is `echo test`; denying `echo` leaves its entry wider than the
    template, and permissions.py refuses it as it would refuse a projects.yaml edit."""
    ps, baselines, src = fixture_project
    alpha = next(p for p in ps if p.id == "alpha")
    got, _ = _compile(alpha, baselines["alpha"], src,
                      [pol("p2", [{"effect": "deny", "kind": "tool", "match": ["Bash(echo *)"]}])])
    assert got["autopilot_permissions"]["error"].startswith("ConfigError")


def test_a_type_policy_that_widens_past_the_template_is_refused(fixture_project):
    """A tool allow from a type is in the compiled template, so it renders; the narrowing check
    still holds the guardrail denies in place."""
    ps, baselines, src = fixture_project
    alpha = next(p for p in ps if p.id == "alpha")
    sneaky = pol("t", [{"effect": "allow", "kind": "tool", "match": ["Bash(git push *)"]}])
    L = ev.Layers(GUARD, (BASE, sneaky), (schema.validate(legacy_policy(baselines["alpha"]), REG),))
    got, _ = compiler.compile_project(alpha, L, UNI, baselines["alpha"]["mcp"], src)
    s = got["autopilot_permissions"]["settings"]["permissions"]
    assert "Bash(git push *)" in s["allow"] and "Bash(git push *)" in s["deny"]   # deny beats allow


# ---- every committed baseline ------------------------------------------------------------------

BASELINES = sorted(p for p in glob.glob(os.path.join(compiler.BASELINE, "*.json")) if not p.endswith("_global.json"))


@pytest.fixture(scope="module")
def live():
    if not os.path.isdir(os.path.expanduser(projects.HUB_BASE)):
        pytest.skip("no hub checkout")
    from nomad_projects import profiles
    reg = schema.Registry.live()
    ps = {p.id: p for p in projects.load(os.path.join(NOMAD, "projects.yaml"),
                                         base=os.path.expanduser(projects.HUB_BASE))}
    return ps, reg, profiles.load_policies(reg), compiler.Universe.live()


@pytest.mark.parametrize("path", BASELINES, ids=lambda p: os.path.basename(p)[:-5])
def test_every_project_compiles_to_its_baseline(path, live):
    ps, reg, policies, uni = live
    with open(path) as f:
        base = json.load(f)
    p = ps.get(base["id"])
    if p is None:
        pytest.skip(f"{base['id']} is not in projects.yaml")
    if not os.path.isdir(p.repo):
        pytest.skip(f"{p.repo} is not checked out here")
    from nomad_policy import tag      # a tagged project compiles through its saved legacy-<id> (#1393)
    layers = compiler.legacy_layers(base, p.type, policies, reg, tuple(policies[x] for x in p.policies),
                                    tag.current_legacy(base))
    got, _ = compiler.compile_project(p, layers, uni, base["mcp"])
    assert compiler.render(got) == compiler.render(compiler.baseline_fields(base))


def test_the_gateway_universe_matches_the_registry_rows():
    uni = compiler.Universe.live()
    assert {"alpaca", "figma"} <= uni.gateway and "nomad-pm" not in uni.gateway
    assert uni.gateway <= uni.repo and uni.local <= uni.repo


def test_layers_for_puts_the_profile_policies_in_the_type_layer():
    from nomad_projects import profiles
    policies = profiles.load_policies(schema.Registry.live())
    L = compiler.layers_for("website", policies)
    assert [p.id for p in L.guardrail] == list(compiler.GUARDRAILS)
    assert L.type[0].id == compiler.WORKER_BASE
    assert [p.id for p in L.type[1:]] == list(profiles.load("website").policies)
    assert compiler.layers_for("generic", policies).type == (policies[compiler.WORKER_BASE],)


def test_an_attached_project_policy_is_compiled_at_the_project_layer():
    """#1373: `policies: [engine-unity]` in projects.yaml reaches the autopilot settings."""
    from nomad_projects import profiles
    pols = profiles.load_policies()
    base = compiler.layers_for("game", pols)
    unity = compiler.layers_for("game", pols, (pols["engine-unity"],))
    assert "**/*.unity" not in (ev.values(base).get("deny_edit_globs") or ())
    assert "**/*.unity" in ev.values(unity)["deny_edit_globs"]
