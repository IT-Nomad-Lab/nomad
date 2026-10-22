import importlib.util
import json
import os
import tomllib

import pytest

_spec = importlib.util.spec_from_file_location("mcp_sync", os.path.join(os.path.dirname(__file__), "mcp-sync.py"))
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


@pytest.fixture
def env(tmp_path, monkeypatch):
    p = {"project": tmp_path / "mcp.json", "user": tmp_path / "claude.json",
         "codex": tmp_path / "codex" / "config.toml", "local": tmp_path / "out" / "mcp-all.json",
         "backends": tmp_path / "backends.json"}
    monkeypatch.setattr(ms, "PROJECT_MCP", str(p["project"]))
    monkeypatch.setattr(ms, "USER_CLAUDE", str(p["user"]))
    monkeypatch.setattr(ms, "CODEX_TOML", str(p["codex"]))
    monkeypatch.setattr(ms, "LOCAL_JSON", str(p["local"]))
    monkeypatch.setattr(ms, "BACKENDS", str(p["backends"]))
    p["project"].write_text(json.dumps({"mcpServers": {
        "nomad-pm": {"command": "/py", "args": ["/pm.py"], "env": {"NOMAD_PM_AS": "dev"}},
        "supabase": {"type": "http", "url": "https://mcp.supabase.com/mcp?a=1&b=2"}}}))
    p["user"].write_text(json.dumps({"mcpServers": {
        "nomad-pm": {"command": "/other"},                      # same name: project wins
        "elevenlabs": {"command": "uvx", "args": ["elevenlabs-mcp"], "env": {"K": 'a"b'}}},
        "projects": {}}))
    return p


def test_registry_merges_project_first_and_keeps_both_shapes(env):
    r = ms.registry()
    assert set(r) == {"nomad-pm", "supabase", "elevenlabs"}
    assert r["nomad-pm"]["command"] == "/py"
    assert r["supabase"] == {"url": "https://mcp.supabase.com/mcp?a=1&b=2"}


def test_sync_writes_valid_toml_with_every_server_and_the_local_json(env):
    env["codex"].parent.mkdir()
    env["codex"].write_text('model = "x"\n\n[projects."/tmp/a"]\ntrust_level = "trusted"\n')
    r = ms.sync()
    assert r == {"codex": True, "local": True, "skipped": []}
    t = tomllib.loads(env["codex"].read_text())
    assert t["model"] == "x" and t["projects"]["/tmp/a"]["trust_level"] == "trusted"
    assert t["mcp_oauth_credentials_store"] == "file"
    s = t["mcp_servers"]
    assert s["nomad-pm"] == {"command": "/py", "args": ["/pm.py"], "env": {"NOMAD_PM_AS": "dev"},
                             "default_tools_approval_mode": "approve"}
    assert all(v["default_tools_approval_mode"] == "approve" for v in s.values())
    assert s["supabase"]["url"].endswith("?a=1&b=2")
    assert s["elevenlabs"]["env"]["K"] == 'a"b'
    local = json.loads(env["local"].read_text())["mcpServers"]
    assert local["supabase"]["type"] == "http" and local["nomad-pm"]["command"] == "/py"
    assert oct(os.stat(env["local"]).st_mode)[-3:] == "600"


def test_second_run_changes_nothing_and_check_agrees(env):
    ms.sync()
    assert ms.sync() == {"codex": False, "local": False, "skipped": []}
    assert ms.main(["--check"]) == 0


def test_new_server_is_picked_up_and_removed_one_disappears(env):
    ms.sync()
    d = json.loads(env["project"].read_text())
    d["mcpServers"]["figma"] = {"type": "http", "url": "https://mcp.figma.com/mcp"}
    del d["mcpServers"]["supabase"]
    env["project"].write_text(json.dumps(d))
    assert ms.main(["--check"]) == 1
    ms.sync()
    s = tomllib.loads(env["codex"].read_text())["mcp_servers"]
    assert "figma" in s and "supabase" not in s


def test_withheld_server_goes_to_the_other_backend_only(env):
    env["backends"].write_text(json.dumps({"codex": ["elevenlabs"]}))
    ms.sync()
    assert "elevenlabs" not in tomllib.loads(env["codex"].read_text())["mcp_servers"]
    assert "elevenlabs" in json.loads(env["local"].read_text())["mcpServers"]


def test_a_table_defined_by_hand_is_left_alone(env):
    env["codex"].parent.mkdir()
    env["codex"].write_text('[mcp_servers.nomad-pm]\ncommand = "/mine"\n')
    r = ms.sync()
    assert r["skipped"] == ["nomad-pm"]
    s = tomllib.loads(env["codex"].read_text())["mcp_servers"]
    assert s["nomad-pm"]["command"] == "/mine" and "supabase" in s


def test_real_registry_produces_parseable_toml():
    # the operator's actual files, read only; nothing is written
    toml, local, _ = ms.outputs()
    tomllib.loads(toml)
    assert json.loads(local)["mcpServers"]


def test_every_server_by_hand_still_closes_the_block_and_is_idempotent(env):
    env["codex"].parent.mkdir()
    env["codex"].write_text("".join(f'[mcp_servers.{n}]\ncommand = "/mine"\n\n' for n in ("nomad-pm", "supabase", "elevenlabs")))
    ms.sync()
    t = env["codex"].read_text()
    assert t.count(ms.BEGIN) == 1 and t.count(ms.END) == 1
    assert ms.sync(check=True)["codex"] is False
    ms.sync()
    assert env["codex"].read_text() == t
