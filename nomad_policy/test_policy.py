"""Tests for nomad_policy: one test per rule of PRD docs/prd/project-types-and-inline-asks.md 6.5.

Offline: names come from a fixture Registry, not the live MCP registry.
Run: uv run --no-project --with pytest --with pyyaml python -m pytest nomad_policy -q
"""
import glob
import json
import os

import pytest
import yaml

from nomad_policy import evaluator as ev
from nomad_policy import schema
from nomad_policy.legacy import channel_servers, legacy_policy

NOMAD = schema.NOMAD
REG = schema.Registry(frozenset({"figma", "nomad-pm", "nomad-image", "hostinger-dns", "supabase"}),
                      frozenset({"agent-docs-audit", "supabase"}))


def pol(pid, role="permission", statements=(), **kw):
    return schema.validate({"id": pid, "role": role, "statements": list(statements), **kw}, REG)


def st(effect, kind, *match, **kw):
    return {"effect": effect, "kind": kind, "match": list(match), **kw}


def layers(guardrail=(), type=(), project=()):
    return ev.Layers(tuple(guardrail), tuple(type), tuple(project))


# 1. A policy is a list of statements {effect, kind, match, when} plus settings.
def test_statement_and_settings_parse():
    p = pol("p", statements=[st("allow", "mcp", "figma", "nomad-*", sid="s1", when={"lane": "code", "tier": ["green", "yellow"]})],
            settings={"cap": 2, "model": "opus", "max_budget_usd": 1.5, "deny_edit_globs": ["secrets/**"],
                      "pre_merge_gate": ["make test", {"run": "make build", "env": {"A": "1"}}]})
    s = p.statements[0]
    assert (s.effect, s.kind, s.match, s.sid) == ("allow", "mcp", ("figma", "nomad-*"), "s1")
    assert s.when == {"lane": ("code",), "tier": ("green", "yellow")}
    assert p.settings["cap"] == 2


@pytest.mark.parametrize("kind", schema.KINDS)
def test_every_kind_is_accepted(kind):
    name = {"mcp": "figma", "skill": "supabase"}.get(kind, "anything")
    assert pol("p", statements=[st("allow", kind, name)]).statements[0].kind == kind


# Validator: unknown MCP, skill or field refused, with the field named.
@pytest.mark.parametrize("raw, field", [
    ({"statements": [st("allow", "mcp", "nope")]}, "statements[0].match[0]"),
    ({"statements": [st("allow", "skill", "nope")]}, "statements[0].match[0]"),
    ({"statements": [st("allow", "mcp", "zzz-*")]}, "statements[0].match[0]"),
    ({"bogus": 1}, "bogus"),
    ({"statements": [{**st("allow", "tool", "Bash"), "extra": 1}]}, "statements[0].extra"),
    ({"statements": [st("allow", "tool", "Bash", when={"color": "red"})]}, "statements[0].when.color"),
    ({"statements": [st("allow", "tool", "Bash", when={"tier": "purple"})]}, "statements[0].when.tier"),
    ({"statements": [st("maybe", "tool", "Bash")]}, "statements[0].effect"),
    ({"statements": [st("allow", "widget", "x")]}, "statements[0].kind"),
    ({"statements": [st("allow", "tool")]}, "statements[0].match"),
    ({"settings": {"colour": 1}}, "settings.colour"),
    ({"settings": {"cap": 0}}, "settings.cap"),
    ({"settings": {"cap": True}}, "settings.cap"),
    ({"settings": {"model": "claude-opus-4"}}, "settings.model"),
    ({"settings": {"max_budget_usd": -1}}, "settings.max_budget_usd"),
    ({"settings": {"pre_merge_gate": [{"nope": 1}]}}, "settings.pre_merge_gate[0]"),
    ({"role": "admin"}, "role"),
])
def test_validator_refuses_and_names_the_field(raw, field):
    with pytest.raises(schema.PolicyError, match=field.replace("[", r"\[").replace("]", r"\]")):
        schema.validate({"id": "p", "role": "permission", **raw}, REG)


def test_file_loader_checks_the_file_name_and_yaml(tmp_path):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump({"id": "b", "role": "permission"}))
    with pytest.raises(schema.PolicyError, match="must equal the file name"):
        schema.load_dir(REG, str(tmp_path))
    (tmp_path / "a.yaml").write_text(yaml.safe_dump({"id": "a", "role": "permission",
                                                      "statements": [st("allow", "mcp", "figma")]}))
    (tmp_path / "bad.yaml").write_text("id: [")
    with pytest.raises(schema.PolicyError, match="not valid YAML"):
        schema.load_dir(REG, str(tmp_path))
    (tmp_path / "bad.yaml").unlink()
    assert list(schema.load_dir(REG, str(tmp_path))) == ["a"]


def test_models_match_autopilot():
    from autopilot import projects
    assert schema.MODELS == projects.MODELS


def test_live_registry_knows_the_repo_skill_and_the_meta_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SYNC_PROJECT", str(tmp_path / "none.json"))
    monkeypatch.setenv("MCP_SYNC_USER", str(tmp_path / "none2.json"))
    reg = schema.Registry.live(home=str(tmp_path))
    assert "agent-docs-audit" in reg.skills
    assert {"nomad-pm", "figma", "hostinger-dns"} <= reg.mcps


def _skill(root, *parts):
    d = root.joinpath(*parts)
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: x\n---\n")


def test_live_registry_knows_installed_and_synced_plugin_skills(tmp_path, monkeypatch):
    """#1404: plugin skills are `<plugin>:<skill>`, from installed_plugins.json and synced/."""
    import json
    monkeypatch.setenv("MCP_SYNC_PROJECT", str(tmp_path / "none.json"))
    monkeypatch.setenv("MCP_SYNC_USER", str(tmp_path / "none2.json"))
    plugins = tmp_path / ".claude" / "plugins"
    fd = plugins / "cache" / "official" / "frontend-design" / "abc123"
    _skill(fd, "skills", "frontend-design")
    (fd / "skills" / "notes").mkdir()                              # no SKILL.md: not a skill
    named = plugins / "cache" / "official" / "dk" / "1.0"           # plugin.json renames it
    _skill(named, "skills", "query")
    (named / ".claude-plugin").mkdir()
    (named / ".claude-plugin" / "plugin.json").write_text('{"name": "duckdb-skills"}')
    (plugins / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "frontend-design@official": [{"scope": "user", "installPath": str(fd)}],
        "dk@official": [{"scope": "user", "installPath": str(named)}],
        "gone@official": [{"scope": "user", "installPath": str(tmp_path / "missing")}]}}))
    _skill(plugins, "synced", "org1", "searchfit-seo~g2", "skills", "technical-seo")
    _skill(plugins, ".trash", "1-x", "old", "skills", "stale")
    reg = schema.Registry.live(home=str(tmp_path))
    plugin = {s for s in reg.skills if ":" in s}
    assert plugin == {"frontend-design:frontend-design", "duckdb-skills:query", "searchfit-seo:technical-seo"}
    schema.validate({"id": "p", "role": "permission", "statements": [
        {"effect": "allow", "kind": "skill", "match": ["frontend-design:frontend-design", "searchfit-seo:*"]}]}, reg)
    with pytest.raises(schema.PolicyError, match="unknown skill 'old:stale'"):
        schema.validate({"id": "p", "role": "permission", "statements": [
            {"effect": "allow", "kind": "skill", "match": ["old:stale"]}]}, reg)


def test_no_plugins_dir_means_no_plugin_skills(tmp_path):
    assert schema.plugin_skills(str(tmp_path / "nothing")) == set()
    (tmp_path / "installed_plugins.json").write_text("not json")
    assert schema.plugin_skills(str(tmp_path)) == set()


# Layers and attachment: guardrail policies only in layer 1, permission policies in 2 and 3.
def test_layers_take_the_right_roles():
    g, p = pol("g", "guardrail"), pol("p")
    layers([g], [p], [p])
    with pytest.raises(ValueError, match="guardrail layer"):
        layers([p])
    with pytest.raises(ValueError, match="type layer"):
        layers(type=[g])
    with pytest.raises(ValueError, match="project layer"):
        layers(project=[g])


def test_guardrail_holds_only_denies_and_only_it_locks():
    with pytest.raises(schema.PolicyError, match="statements\\[0\\].effect"):
        pol("g", "guardrail", statements=[st("allow", "mcp", "figma")])
    with pytest.raises(schema.PolicyError, match="only a guardrail can lock"):
        pol("p", settings={"cap": 1}, locks=["cap"])
    with pytest.raises(schema.PolicyError, match="gives it no value"):
        pol("g", "guardrail", locks=["cap"])


# Evaluation: default deny.
def test_default_deny_without_any_policy():
    d = ev.decide(layers(), "mcp", "figma")
    assert (d.effect, d.reason, d.policy) == ("deny", "default-deny", "")


def test_default_deny_when_no_statement_matches():
    d = ev.decide(layers(type=[pol("t", statements=[st("allow", "mcp", "figma")])]), "mcp", "supabase")
    assert d.reason == "default-deny"
    d = ev.decide(layers(type=[pol("t", statements=[st("allow", "mcp", "figma")])]), "skill", "figma")
    assert d.reason == "default-deny"           # same name, other kind


# An allow counts only if a type or project policy grants it.
def test_a_type_or_project_policy_grants():
    t = pol("t", statements=[st("allow", "mcp", "figma")])
    p = pol("p", statements=[st("allow", "skill", "agent-*")])
    L = layers(type=[t], project=[p])
    assert ev.decide(L, "mcp", "figma") == ev.Decision("allow", "allowed", "type", "t", 0, "")
    assert ev.decide(L, "skill", "agent-docs-audit").layer == "project"
    assert ev.decide(layers(project=[p]), "mcp", "figma").reason == "default-deny"


def test_a_guardrail_alone_never_grants():
    g = pol("g", "guardrail", statements=[st("deny", "mcp", "hostinger-*")])
    assert not ev.decide(layers([g]), "mcp", "figma").allowed


# An explicit deny in any layer wins.
@pytest.mark.parametrize("deny_layer, allow_layer", [
    ("guardrail", "type"), ("guardrail", "project"), ("type", "project"), ("project", "type"),
    ("type", "type"), ("project", "project"),
])
def test_explicit_deny_in_any_layer_beats_an_allow(deny_layer, allow_layer):
    deny = pol("d", "guardrail" if deny_layer == "guardrail" else "permission",
               statements=[st("deny", "mcp", "hostinger-dns", sid="no-dns")])
    allow = pol("a", statements=[st("allow", "mcp", "hostinger-*")])
    parts = {"guardrail": [], "type": [], "project": []}
    parts[deny_layer].append(deny)
    parts[allow_layer].append(allow)
    d = ev.decide(layers(**parts), "mcp", "hostinger-dns")
    assert (d.effect, d.reason, d.layer, d.policy, d.sid) == ("deny", "explicit-deny", deny_layer, "d", "no-dns")
    assert ev.decide(layers(**parts), "mcp", "hostinger-vps").allowed


def test_deny_beats_allow_inside_one_policy_regardless_of_order():
    for order in (("allow", "deny"), ("deny", "allow")):
        p = pol("p", statements=[st(e, "tool", "Bash(git push*)") for e in order])
        assert ev.decide(layers(project=[p]), "tool", "Bash(git push*)").effect == "deny"


def test_deny_only_hits_what_it_matches():
    g = pol("g", "guardrail", statements=[st("deny", "mcp", "hostinger-dns")])
    t = pol("t", statements=[st("allow", "mcp", "hostinger-*")])
    L = layers([g], [t])
    assert ev.decide(L, "mcp", "hostinger-dns").effect == "deny"
    assert ev.decide(L, "mcp", "hostinger-vps").allowed        # the glob still grants the rest


# `when`: lane, tier, task type.
def test_when_conditions_must_all_match():
    p = pol("p", statements=[st("allow", "mcp", "figma", when={"lane": "code", "tier": ["green", "yellow"],
                                                               "task_type": "feat*"})])
    L = layers(project=[p])
    ok = {"lane": "code", "tier": "green", "task_type": "feature"}
    assert ev.decide(L, "mcp", "figma", ok).allowed
    for key, bad in (("lane", "media"), ("tier", "red"), ("task_type", "fix")):
        assert not ev.decide(L, "mcp", "figma", {**ok, key: bad}).allowed


def test_missing_context_fails_closed():
    allow = pol("a", statements=[st("allow", "mcp", "figma", when={"tier": "green"})])
    deny = pol("d", statements=[st("deny", "mcp", "figma", when={"tier": "red"})])
    assert not ev.decide(layers(project=[allow]), "mcp", "figma", {}).allowed        # allow needs the fact
    both = layers(project=[pol("b", statements=[st("allow", "mcp", "figma")]), deny])
    assert ev.decide(both, "mcp", "figma", {}).effect == "deny"                       # deny assumes it
    assert ev.decide(both, "mcp", "figma", {"tier": "green"}).allowed


# Settings: the most specific layer wins.
def test_settings_most_specific_layer_wins():
    g = pol("g", "guardrail", settings={"cap": 5, "model": "haiku"})
    t = pol("t", settings={"cap": 3, "max_budget_usd": 2})
    p = pol("p", settings={"cap": 1})
    got = ev.effective_settings(layers([g], [t], [p]))
    assert (got["cap"].value, got["cap"].layer, got["cap"].policy, got["cap"].locked) == (1, "project", "p", False)
    assert (got["model"].value, got["model"].layer) == ("haiku", "guardrail")
    assert (got["max_budget_usd"].value, got["max_budget_usd"].layer) == (2, "type")
    assert "deny_edit_globs" not in got


def test_settings_later_policy_wins_inside_a_layer():
    a, b = pol("a", settings={"cap": 1}), pol("b", settings={"cap": 2})
    assert ev.values(layers(type=[a, b])) == {"cap": 2}
    assert ev.values(layers(type=[b, a])) == {"cap": 1}


# ... unless a guardrail locks the value.
def test_a_guardrail_lock_beats_type_and_project():
    g = pol("g", "guardrail", settings={"deny_edit_globs": [".claude/hooks/**"], "cap": 4}, locks=["deny_edit_globs"])
    t = pol("t", settings={"deny_edit_globs": []})
    p = pol("p", settings={"deny_edit_globs": ["x/**"], "cap": 2})
    got = ev.effective_settings(layers([g], [t], [p]))
    s = got["deny_edit_globs"]
    assert (s.value, s.layer, s.policy, s.locked) == ([".claude/hooks/**"], "guardrail", "g", True)
    assert [(layer, pid) for layer, pid, _ in s.ignored] == [("type", "t"), ("project", "p")]
    assert got["cap"].value == 2 and not got["cap"].locked       # an unlocked guardrail value is only a default


def test_lock_with_the_same_value_is_not_a_conflict():
    g = pol("g", "guardrail", settings={"cap": 3}, locks=["cap"])
    s = ev.effective_settings(layers([g], project=[pol("p", settings={"cap": 3})]))["cap"]
    assert s.value == 3 and s.locked and s.ignored == ()


def test_lock_stands_when_a_later_guardrail_restates_the_key():
    a = pol("a", "guardrail", settings={"cap": 3}, locks=["cap"])
    b = pol("b", "guardrail", settings={"cap": 2})
    s = ev.effective_settings(layers([a, b], project=[pol("p", settings={"cap": 9})]))["cap"]
    assert (s.value, s.policy, s.locked) == (2, "b", True)


# Tagging keeps today's configuration: legacy-<project> built from the #1389 baselines.
BASELINES = sorted(p for p in glob.glob(os.path.join(NOMAD, "policies", "baseline", "*.json"))
                   if not p.endswith("_global.json"))


def test_there_are_baselines():
    assert BASELINES


@pytest.mark.parametrize("path", BASELINES, ids=lambda p: os.path.basename(p)[:-5])
def test_legacy_policy_reproduces_the_baseline(path):
    with open(path) as f:
        base = json.load(f)
    by = channel_servers(base)
    names = set().union(*by.values()) if by else set()
    reg = schema.Registry(frozenset(names | {"other"}), frozenset())
    p = schema.validate(legacy_policy(base), reg)
    L = layers(project=[p])
    for c in schema.CHANNELS:                           # what it reaches today, on that channel only
        for name in names:
            assert ev.decide(L, "mcp", name, {"channel": c}).allowed == (name in by.get(c, ())), (c, name)
        assert not ev.decide(L, "mcp", "other", {"channel": c}).allowed
    for name in names:                                  # a request that names no channel gets nothing
        assert not ev.decide(L, "mcp", name).allowed
    got = ev.values(L)
    ap = base["autopilot"]
    if ap is None:
        assert got == {}
        return
    assert got.get("cap") == ap["cap"]
    assert got.get("model", "") == ap["model"]
    assert got.get("max_budget_usd") == ap["max_budget_usd"]
    assert got.get("deny_edit_globs", []) == ap["deny_edit_globs"]
    for key in ("pre_merge_gate", "milestone_gate"):
        assert [s["run"] for s in got.get(key, [])] == [c["run"] for c in ap[key]]


def test_the_gateway_connection_is_not_granted_as_a_server():
    base = {"id": "x", "autopilot": None,
            "mcp": {"gateway": {"servers": ["alpaca"]}, "repo_mcp_json": ["mcp-gateway", "nomad-pm"]}}
    reg = schema.Registry(frozenset({"alpaca", "nomad-pm"}), frozenset())   # live META has no mcp-gateway
    L = layers(project=[schema.validate(legacy_policy(base), reg)])
    assert ev.decide(L, "mcp", "alpaca", {"channel": "gateway"}).allowed
    assert ev.decide(L, "mcp", "nomad-pm", {"channel": "repo"}).allowed
    assert not ev.decide(L, "mcp", "alpaca", {"channel": "repo"}).allowed
    for c in schema.CHANNELS:
        assert not ev.decide(L, "mcp", "mcp-gateway", {"channel": c}).allowed


def test_a_channel_outside_the_three_is_refused():
    with pytest.raises(schema.PolicyError, match="when.channel"):
        schema.validate({"id": "x", "role": "permission", "statements": [
            {"effect": "allow", "kind": "mcp", "match": ["alpaca"], "when": {"channel": "email"}}]},
            schema.Registry(frozenset({"alpaca"}), frozenset()))
