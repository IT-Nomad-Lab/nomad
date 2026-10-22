"""Tests for nomad_policy/tag.py (#1393): the A7 gate (tagging every current project with every
type gives an empty diff), holds and proposals, refusal, save and accept on temp copies.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest nomad_policy -q
"""
import dataclasses
import json
import os

import pytest
import yaml

from autopilot import projects
from nomad_policy import compiler, schema, tag
from nomad_projects import profiles

NOMAD = schema.NOMAD
TYPES = sorted(f[:-5] for f in os.listdir(os.path.join(NOMAD, "profiles")) if f.endswith(".yaml"))


@pytest.fixture(scope="module")
def live():
    if not os.path.isdir(os.path.expanduser(projects.HUB_BASE)):
        pytest.skip("no hub checkout")
    reg = schema.Registry.live()
    ps = [p for p in projects.load(os.path.join(NOMAD, "projects.yaml"),
                                   base=os.path.expanduser(projects.HUB_BASE)) if os.path.isdir(p.repo)]
    return ps, reg, profiles.load_policies(reg), compiler.Universe.live()


def _base(pid):
    with open(os.path.join(compiler.BASELINE, pid + ".json")) as f:
        return json.load(f)


def _plan(live, p, t, policies=None, root=tag.LEGACY_DIR):
    _, reg, pols, uni = live
    return tag.plan(p, t, _base(p.id), policies or pols, reg, uni, root)


def _one(live, pid):
    return next(p for p in live[0] if p.id == pid)


# ---- A7: every current project, every type -----------------------------------------------------

def test_tagging_every_project_with_every_type_gives_an_empty_diff(live):
    ps = live[0]
    assert ps
    bad = []
    for p in ps:
        for t in TYPES:
            pl = _plan(live, p, t)
            if not pl.ok or pl.diff():
                bad.append((p.id, t, pl.refused, pl.diff()[:6]))
            assert pl.before == compiler.baseline_fields(_base(p.id)) or p.type != "generic"
    assert not bad


def test_what_a_type_adds_is_held_and_proposed(live):
    p = _one(live, "crypto-trading-bot")               # has a gateway key, so all three channels
    pl = _plan(live, p, "website")
    ids = {c.id for c in pl.proposed}
    assert {"mcp:gateway:+figma", "mcp:local:+figma", "mcp:repo:+figma"} <= ids
    holds = tag.holds(pl.legacy)
    assert all("figma" in holds[c] for c in ("gateway", "local", "repo"))
    assert "figma" not in json.dumps(pl.after["mcp"])          # held: the tag gives nothing new


def test_the_generic_type_proposes_nothing(live):
    pl = _plan(live, _one(live, "nomad-crm"), "generic")
    assert pl.ok and not pl.proposed and not tag.holds(pl.legacy)


# ---- refusal -----------------------------------------------------------------------------------

def test_a_type_that_removes_a_server_is_refused(live, tmp_path):
    p = _one(live, "crypto-trading-bot")
    base = _base(p.id)
    server = sorted(base["mcp"]["gateway"]["servers"])[0]
    _, reg, pols, _ = live
    deny = schema.validate({"id": "website-default", "role": "permission",
                            "statements": [{"effect": "deny", "kind": "mcp", "match": [server]}]}, reg)
    pl = _plan(live, p, "website", {**pols, "website-default": deny})
    assert not pl.ok and any(f"-{server}" in why for why in pl.refused)
    with pytest.raises(ValueError, match="refused"):
        tag.save(pl, str(tmp_path / "projects.yaml"), str(tmp_path))


def test_a_plan_with_a_diff_is_refused_on_save(live, tmp_path):
    pl = _plan(live, _one(live, "nomad-crm"), "website")
    after = dict(pl.after, autopilot={"cap": 99})
    with pytest.raises(ValueError, match="diff is not empty"):
        tag.save(dataclasses.replace(pl, after=after), str(tmp_path / "projects.yaml"), str(tmp_path))


# ---- save and accept, on temp copies -----------------------------------------------------------

def test_save_then_compile_keeps_the_configuration_and_accept_lifts_one_hold(live, tmp_path):
    reg_file = tmp_path / "projects.yaml"
    with open(os.path.join(NOMAD, "projects.yaml")) as f:
        reg_file.write_text(f.read())
    p = _one(live, "crypto-trading-bot")
    pl = _plan(live, p, "website", root=str(tmp_path))
    tag.save(pl, str(reg_file), str(tmp_path))

    saved = yaml.safe_load((tmp_path / "legacy-crypto-trading-bot.yaml").read_text())
    assert saved == pl.legacy
    tagged = next(x for x in projects.load(str(reg_file), base=os.path.expanduser(projects.HUB_BASE))
                  if x.id == "crypto-trading-bot")
    assert tagged.type == "website"
    again = _plan(live, tagged, "website", root=str(tmp_path))   # re-planning a saved tag: nothing new
    assert again.ok and not again.diff() and again.before == pl.before

    tag.accept("crypto-trading-bot", "mcp:gateway:+figma", str(tmp_path))
    lifted = yaml.safe_load((tmp_path / "legacy-crypto-trading-bot.yaml").read_text())
    assert "figma" not in tag.holds(lifted).get("gateway", set())
    assert "figma" in tag.holds(lifted)["local"]               # the other channels stay held
    with pytest.raises(ValueError, match="no held change"):
        tag.accept("crypto-trading-bot", "mcp:gateway:+figma", str(tmp_path))


def test_current_legacy_falls_back_to_the_baseline(tmp_path):
    base = _base("nomad-crm")
    from nomad_policy import legacy
    assert tag.current_legacy(base, str(tmp_path)) == legacy.legacy_policy(base)


# ---- projects.yaml edit ------------------------------------------------------------------------

TEXT = """projects:
  # a comment
  - id: alpha
    path: alpha      # keep me
  - id: beta
    path: beta
    type: game

    autopilot:
      cap: 1
other: 1
"""


def test_set_type_adds_after_the_id_line_and_keeps_comments():
    out = tag.set_type(TEXT, "alpha", "website")
    assert "  - id: alpha\n    type: website\n    path: alpha      # keep me\n" in out
    assert out.replace("    type: website\n", "", 1) == TEXT


def test_set_type_replaces_an_existing_type():
    out = tag.set_type(TEXT, "beta", "service")
    assert "    type: service\n" in out and "type: game" not in out
    assert out.count("type:") == 1


def test_set_type_on_an_unknown_project_raises():
    with pytest.raises(ValueError, match="no entry"):
        tag.set_type(TEXT, "gamma", "website")
