"""Tests for scripts/config-snapshot.py: fixtures only, no live gateway, grants or registry.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest scripts/test_config_snapshot.py -q
"""
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("config_snapshot", os.path.join(HERE, "config-snapshot.py"))
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

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
    local_only: true
"""


@pytest.fixture
def env(tmp_path):
    for d in ("alpha", "beta"):
        (tmp_path / d).mkdir()
    (tmp_path / "alpha" / ".mcp.json").write_text(json.dumps({"mcpServers": {"b": {}, "a": {}}}))
    reg = tmp_path / "projects.yaml"
    reg.write_text(REGISTRY)
    state = {"servers": ["nomad-image"], "grants": {"default": ["x"], "projects": {"alpha": ["y"]}}}
    src = cs.Sources(gateway=lambda p: (p == "alpha", state["servers"] if p == "alpha" else []),
                     grants=lambda: state["grants"], state_dir=str(tmp_path / "state"),
                     default_branch="main")
    out = tmp_path / "baseline"
    return reg, src, out, state, tmp_path


def snap(env):
    reg, src, _, _, tmp = env
    return cs.snapshot(str(reg), src, base=str(tmp))


def test_every_project_has_a_file_and_content(env):
    s = snap(env)
    assert sorted(s) == ["_global", "alpha", "beta"]
    a = s["alpha"]
    assert a["autopilot"]["cap"] == 2 and a["autopilot"]["model"] == "sonnet"
    assert a["mcp"]["repo_mcp_json"] == ["a", "b"]
    assert a["mcp"]["local_model_set"] == ["x", "y"]
    assert a["mcp"]["gateway"] == {"key_present": True, "servers": ["nomad-image"]}
    assert "Edit(secrets/**)" in a["autopilot_permissions"]["settings"]["permissions"]["deny"]
    assert s["beta"]["autopilot"] is None and s["beta"]["autopilot_permissions"] is None
    assert s["beta"]["local_only"] is True and "refused" in s["beta"]["agent_bridge"]


def test_deterministic(env):
    _, _, out, _, _ = env
    cs.write(snap(env), str(out))
    first = {f: (out / f).read_text() for f in os.listdir(out)}
    cs.write(snap(env), str(out))
    assert first == {f: (out / f).read_text() for f in os.listdir(out)}
    for text in first.values():
        d = json.loads(text)
        assert text == cs.render(d)          # sorted, stable serialisation


def test_check_passes_after_write(env):
    _, _, out, _, _ = env
    cs.write(snap(env), str(out))
    assert cs.check(snap(env), str(out)) == ([], [])


@pytest.mark.parametrize("change", ["grant", "gateway", "registry", "mcpjson"])
def test_check_detects_change(env, change):
    reg, src, out, state, tmp = env
    cs.write(snap(env), str(out))
    if change == "grant":
        state["grants"]["projects"]["alpha"].append("z")
    elif change == "gateway":
        state["servers"] = ["nomad-image", "nomad-audio"]
    elif change == "registry":
        reg.write_text(REGISTRY.replace("cap: 2", "cap: 4"))
    else:
        (tmp / "alpha" / ".mcp.json").write_text(json.dumps({"mcpServers": {"c": {}}}))
    diff, _ = cs.check(snap(env), str(out))
    assert diff and any(l.startswith(("+", "-")) for l in diff)


def test_check_detects_new_and_removed_project(env):
    reg, _, out, _, _ = env
    cs.write(snap(env), str(out))
    reg.write_text(REGISTRY + "  - id: gamma\n    path: beta\n")
    diff, _ = cs.check(snap(env), str(out))
    assert any("gamma" in l for l in diff)
    reg.write_text(REGISTRY.split("  - id: beta")[0])
    diff, _ = cs.check(snap(env), str(out))
    assert any("beta" in l for l in diff)


def test_unreachable_gateway_is_skipped_not_a_diff(env):
    reg, src, out, state, tmp = env
    cs.write(snap(env), str(out))
    src.gateway = lambda p: (p == "alpha", None)
    diff, notes = cs.check(snap(env), str(out))
    assert diff == [] and notes


def test_main_exit_codes(env, capsys, monkeypatch):
    reg, src, out, state, tmp = env
    monkeypatch.setattr(cs, "Sources", lambda: src)
    monkeypatch.setattr(cs.projects, "HUB_BASE", str(tmp))
    args = ["--registry", str(reg), "--out", str(out)]
    assert cs.main(args) == 0
    assert cs.main(args + ["--check"]) == 0
    state["grants"]["default"].append("new")
    assert cs.main(args + ["--check"]) == 1
    assert "new" in capsys.readouterr().out


def test_no_key_values_in_output(env):
    text = json.dumps(snap(env))
    assert "Bearer" not in text and "sk-" not in text
