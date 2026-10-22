"""Tests for the `ask` effect and the `command` and `path` kinds (#1391, PRD 6.5).

Offline, like test_policy.py. Run: uv run --no-project --with pytest --with pyyaml python -m pytest nomad_policy -q
"""
import pytest

from nomad_policy import evaluator as ev
from nomad_policy import schema

REG = schema.Registry(frozenset({"figma"}), frozenset())


def pol(pid, role="permission", statements=(), **kw):
    return schema.validate({"id": pid, "role": role, "statements": list(statements), **kw}, REG)


def st(effect, kind, *match, **kw):
    return {"effect": effect, "kind": kind, "match": list(match), **kw}


def layers(guardrail=(), type=(), project=()):
    return ev.Layers(tuple(guardrail), tuple(type), tuple(project))


# The ask effect: deny > ask > allow, a guardrail may hold deny and ask, never allow.
def test_ask_is_a_valid_effect_for_every_role():
    assert pol("g", "guardrail", statements=[st("ask", "tool", "Bash(git push *)")]).statements[0].effect == "ask"
    assert pol("p", statements=[st("ask", "tool", "Bash(git push *)")]).statements[0].effect == "ask"
    with pytest.raises(schema.PolicyError, match=r"statements\[0\].effect"):
        pol("g", "guardrail", statements=[st("allow", "tool", "x")])


def test_a_guardrail_ask_is_not_a_grant():
    g = pol("g", "guardrail", statements=[st("ask", "tool", "Bash(git push *)")])
    d = ev.decide(layers([g]), "tool", "Bash(git push origin)")
    assert (d.effect, d.reason, d.layer, d.policy, d.index) == ("ask", "ask-required", "guardrail", "g", 0)
    assert d.needs_approval and not d.allowed


def test_ask_beats_an_allow_from_any_layer():
    g = pol("g", "guardrail", statements=[st("ask", "tool", "Bash(git push *)", sid="red")])
    p = pol("p", statements=[st("allow", "tool", "Bash(git *)")])
    for lay in (layers([g], [p]), layers([g], project=[p]), layers([g], [p], [p])):
        d = ev.decide(lay, "tool", "Bash(git push origin)")
        assert (d.effect, d.sid) == ("ask", "red")
    assert ev.decide(layers([g], [p]), "tool", "Bash(git status)").allowed


def test_ask_in_a_lower_layer_still_beats_an_allow():
    t = pol("t", statements=[st("allow", "mcp", "figma")])
    p = pol("p", statements=[st("ask", "mcp", "figma")])
    assert ev.decide(layers(type=[t], project=[p]), "mcp", "figma").effect == "ask"


def test_deny_beats_ask():
    g = pol("g", "guardrail", statements=[st("ask", "tool", "Bash(x)"), st("deny", "tool", "Bash(x)")])
    assert ev.decide(layers([g]), "tool", "Bash(x)").effect == "deny"
    a = pol("a", "guardrail", statements=[st("ask", "tool", "Bash(x)")])
    p = pol("p", statements=[st("deny", "tool", "Bash(x)")])
    assert ev.decide(layers([a], project=[p]), "tool", "Bash(x)").reason == "explicit-deny"


def test_ask_only_hits_what_it_matches_and_honours_when():
    g = pol("g", "guardrail", statements=[st("ask", "action", "deploy", when={"tier": "red"})])
    assert ev.decide(layers([g]), "action", "deploy", {"tier": "red"}).effect == "ask"
    assert ev.decide(layers([g]), "action", "deploy", {"tier": "green"}).reason == "default-deny"
    assert ev.decide(layers([g]), "action", "other", {"tier": "red"}).reason == "default-deny"
    assert ev.decide(layers([g]), "action", "deploy").effect == "ask"          # missing context: fail closed


# Kind command: regular expressions.
def test_command_matches_by_regex_case_insensitively():
    g = pol("g", "guardrail", statements=[st("ask", "command", r"\bgit\s+push\b"), st("deny", "command", r"\.pem\b")])
    assert ev.decide(layers([g]), "command", "cd x && GIT  push origin").effect == "ask"
    assert ev.decide(layers([g]), "command", "cat key.pem").effect == "deny"
    assert ev.decide(layers([g]), "command", "git pull").reason == "default-deny"
    assert ev.decide(layers([g]), "tool", "git push").reason == "default-deny"      # other kind


def test_command_pattern_must_compile():
    with pytest.raises(schema.PolicyError, match=r"statements\[0\].match\[0\]"):
        pol("g", "guardrail", statements=[st("deny", "command", "(unclosed")])


# Kind path: globs, a directory entry covers what is beneath it.
def test_path_covers_a_directory_and_globs():
    g = pol("g", "guardrail", statements=[st("deny", "path", ".claude/hooks", ".gitleaks.toml", "**/secrets.*")])
    for hit in (".claude/hooks", ".claude/hooks/guard.py", ".gitleaks.toml", "a/b/secrets.yaml"):
        assert ev.decide(layers([g]), "path", hit).effect == "deny", hit
    for miss in (".claude/hooks-old/x", ".claude/settings.local.json", "src/main.py"):
        assert ev.decide(layers([g]), "path", miss).reason == "default-deny", miss


# Setting default_mode (the template's permissions.defaultMode) locks like any other.
def test_default_mode_setting_locks_like_any_other():
    g = pol("g", "guardrail", settings={"default_mode": "dontAsk"}, locks=["default_mode"])
    p = pol("p", settings={"default_mode": "acceptEdits"})
    s = ev.effective_settings(layers([g], project=[p]))["default_mode"]
    assert (s.value, s.locked, s.ignored) == ("dontAsk", True, (("project", "p", "acceptEdits"),))
    with pytest.raises(schema.PolicyError, match=r"settings.default_mode"):
        pol("p", settings={"default_mode": ""})
