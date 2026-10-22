import copy

import pytest
import yaml

from nomad_policy import evaluator as ev
from nomad_policy import schema
from nomad_projects import profiles as pr

REG = schema.Registry(frozenset({"figma", "nomad-pm"}), frozenset({"demo"}))


def policy(pid, role="permission", **kw):
    return schema.validate({"id": pid, "role": role, **kw}, REG)


def allow(kind, *match):
    return [{"effect": "allow", "kind": kind, "match": list(match)}]


POLICIES = {"web-tools": policy("web-tools", statements=allow("mcp", "figma"), settings={"cap": 2}),
            "game-tools": policy("game-tools", statements=allow("skill", "demo"), settings={"cap": 1}),
            "org-ceiling": policy("org-ceiling", role="guardrail",
                                  statements=[{"effect": "deny", "kind": "mcp", "match": ["figma"]}])}

GOOD = {"id": "website", "title": "Website", "description": "A static or CMS site.",
        "interview": {"ask": [{"id": "domain", "question": "Domain?", "kind": "text"}], "skip": ["db"]},
        "stack": {"language": "typescript"}, "policies": ["web-tools"],
        "agents": [{"role": "frontend-ux", "lane": "dev"}],
        "gates": {"pre_merge": ["build"], "milestone": ["test:e2e"]},
        "files": ["README.md"], "autopilot": {"cap": 2, "model": "sonnet", "lanes": ["Dev"]}}


@pytest.fixture
def skills():
    return POLICIES


def check(data, skills, **kw):
    return pr.validate(data, policies=skills, **kw)


def bad(data, skills, field, **kw):
    with pytest.raises(pr.ProfileError) as e:
        check(data, skills, **kw)
    assert field in str(e.value)
    return str(e.value)


def test_good_profile_validates(skills):
    p = check(GOOD, skills, name="website")
    assert p.policies == ("web-tools",) and p.autopilot["lanes"] == ("dev",)
    assert p.interview["ask"][0]["id"] == "domain" and p.gates["milestone"] == ("test:e2e",)


def test_unknown_field_rejected_and_named(skills):
    assert "polices" in bad({**GOOD, "polices": []}, skills, "unknown field")


def test_unknown_policy_rejected_and_named(skills):
    msg = bad({**GOOD, "policies": ["web-tools", "no-such"]}, skills, "policies[1]")
    assert "no-such" in msg


def test_guardrail_policy_refused_in_a_profile(skills):
    msg = bad({**GOOD, "policies": ["org-ceiling"]}, skills, "policies[0]")
    assert "guardrail" in msg


def test_duplicate_policy_refused(skills):
    bad({**GOOD, "policies": ["web-tools", "web-tools"]}, skills, "policies[1]")


def test_mcps_and_skills_moved_into_policies(skills):
    for old in ("mcps", "skills"):
        assert "policies" in bad({**GOOD, old: ["x"]}, skills, old)


def test_policies_must_be_a_list(skills):
    bad({**GOOD, "policies": "web-tools"}, skills, "policies")


def test_layers_for_gives_the_effective_policy(skills):
    web = check(GOOD, skills, name="website")
    game = check({**GOOD, "id": "game", "policies": ["game-tools"]}, skills, name="game")
    lw, lg = pr.layers_for(web, skills), pr.layers_for(game, skills)
    assert [p.id for p in lw.type] == ["web-tools"] and lw.guardrail == ()
    assert ev.decide(lw, "mcp", "figma").allowed and not ev.decide(lg, "mcp", "figma").allowed
    assert ev.decide(lg, "skill", "demo").allowed and not ev.decide(lw, "skill", "demo").allowed
    assert ev.values(lw) == {"cap": 2} and ev.values(lg) == {"cap": 1}


def test_layers_for_takes_guardrails(skills):
    lw = pr.layers_for(check(GOOD, skills), skills, guardrails=[skills["org-ceiling"]])
    assert ev.decide(lw, "mcp", "figma").reason == "explicit-deny"


def test_layers_for_no_policies_is_empty():
    g = pr.Profile("generic", "Generic", "d")
    assert pr.layers_for(g) == ev.Layers()


def test_unknown_nested_keys_rejected(skills):
    for path, val in [("interview", {"asks": []}), ("gates", {"post": []}),
                      ("autopilot", {"test": "x"}), ("agents", [{"role": "a", "tier": 1}])]:
        d = copy.deepcopy(GOOD)
        d[path] = val
        bad(d, skills, path)


def test_missing_required_and_bad_id(skills):
    d = dict(GOOD)
    del d["title"]
    bad(d, skills, "title")
    bad({**GOOD, "id": "Web Site"}, skills, "id")
    bad(GOOD, skills, "must equal the file name", name="game")


def test_bad_values(skills):
    bad({**GOOD, "autopilot": {"cap": 0}}, skills, "autopilot.cap")
    bad({**GOOD, "autopilot": {"model": "gpt-9"}}, skills, "autopilot.model")
    bad({**GOOD, "autopilot": {"max_budget_usd": -1}}, skills, "max_budget_usd")
    bad({**GOOD, "stack": ["x"]}, skills, "stack")
    bad({**GOOD, "interview": {"ask": [{"id": "a", "question": "q", "kind": "dial"}]}}, skills, "kind")
    bad({**GOOD, "agents": [{"lane": "dev"}]}, skills, "role")
    bad(["not", "a", "mapping"], skills, "expected a mapping")


def test_load_reads_file_and_checks_name(tmp_path, skills):
    skills = {"policies": skills}
    (tmp_path / "website.yaml").write_text(yaml.safe_dump(GOOD))
    assert pr.load("website", str(tmp_path), **skills).title == "Website"
    (tmp_path / "game.yaml").write_text(yaml.safe_dump(GOOD))
    with pytest.raises(pr.ProfileError, match="must equal the file name"):
        pr.load("game", str(tmp_path), **skills)
    with pytest.raises(pr.ProfileError, match="no file"):
        pr.load("nope", str(tmp_path))
    (tmp_path / "broken.yaml").write_text("a: [")
    with pytest.raises(pr.ProfileError, match="invalid YAML"):
        pr.load("broken", str(tmp_path))
    with pytest.raises(pr.ProfileError, match="not a valid type id"):
        pr.load("../x", str(tmp_path))


def test_policy_files_in_types_dir_are_found(tmp_path):
    (tmp_path / "types").mkdir()
    (tmp_path / "types" / "web-tools.yaml").write_text(yaml.safe_dump(
        {"id": "web-tools", "role": "permission", "statements": allow("mcp", "figma")}))
    (tmp_path / "org.yaml").write_text(yaml.safe_dump({"id": "org", "role": "guardrail"}))
    got = pr.load_policies(REG, (str(tmp_path), str(tmp_path / "types")))
    assert set(got) == {"web-tools", "org"}


def test_generic_profile_is_todays_behaviour():
    g = pr.load("generic", policies={})
    assert (g.policies, g.agents, g.files) == ((), (), ())
    assert g.gates == {"milestone": (), "pre_merge": ()} and g.autopilot == {}
    assert pr.layers_for(g, {}) == ev.Layers()


# ---- the shipped profiles (#1368): real MCP registry, real skills, real policy files ----------

TYPES = ["data-or-ai", "game", "generic", "mobile-app", "service", "website"]


def test_shipped_profiles_all_validate():
    got = pr.load_all()
    assert sorted(got) == TYPES
    for p in got.values():
        pr.layers_for(p)                     # every named policy loads into the evaluator


def test_every_type_policy_file_validates_as_permission():
    pols = pr.load_policies(dirs=(pr.TYPE_POLICIES_DIR,))   # policies/ also holds the guardrails (#1391)
    assert pols and all(p.role == "permission" for p in pols.values())


def test_spend_and_prod_servers_are_never_granted_by_a_type():
    off = ("hostinger-dns", "hostinger-vps", "hostinger-hosting", "elevenlabs", "supabase",
           "gmail", "gdrive", "gcalendar")
    for p in pr.load_all().values():
        layers = pr.layers_for(p)
        for name in off:
            assert not ev.decide(layers, "mcp", name).allowed, (p.id, name)


def test_website_and_game_compile_to_different_effective_policies():
    pols = pr.load_all()
    web, game = pr.layers_for(pols["website"]), pr.layers_for(pols["game"])
    assert ev.decide(web, "mcp", "figma").allowed and not ev.decide(game, "mcp", "figma").allowed
    assert ev.values(web) != ev.values(game)
    assert ev.values(game)["cap"] == 1
    assert [p.id for p in web.type] != [p.id for p in game.type]


def test_game_is_engine_neutral_and_unity_comes_from_its_policy():
    game = pr.layers_for(pr.load_all()["game"])
    assert "deny_edit_globs" not in ev.values(game) and "pre_merge_gate" not in ev.values(game)
    unity = ev.Layers(type=game.type, project=(pr.load_policies()["engine-unity"],))
    got = ev.values(unity)
    assert got["cap"] == 1 and "**/*.unity" in got["deny_edit_globs"]
    assert [s["run"] for s in got["pre_merge_gate"]][0] == "scripts/unity.sh compile"


def test_every_profile_names_agents_that_exist():
    import os
    have = {f[:-3] for f in os.listdir(os.path.join(pr.NOMAD, ".claude", "agents"))}
    for p in pr.load_all().values():
        assert {a["role"] for a in p.agents} <= have, p.id


def test_website_and_mobile_app_grant_their_plugin_skills():
    """#1404: plugin skills are named `<plugin>:<skill>` and come from the type's policy."""
    def allowed(t, skill):
        return ev.decide(pr.layers_for(pr.load_all()[t]), "skill", skill).allowed
    assert allowed("website", "frontend-design:frontend-design") and allowed("website", "searchfit-seo:technical-seo")
    assert allowed("mobile-app", "frontend-design:frontend-design")
    assert not allowed("mobile-app", "searchfit-seo:technical-seo") and not allowed("game", "frontend-design:frontend-design")
