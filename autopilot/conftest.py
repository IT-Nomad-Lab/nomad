"""Keep the tests off the operator's ~/.claude.json: every run's mcp.json is copied from a
fixture config holding a stand-in nomad-pm server."""
import json

import pytest

FAKE_PM = {"type": "stdio", "command": "/bin/true", "args": [], "env": {"NOMAD_PM_AS": "test"}}


@pytest.fixture(autouse=True)
def mcp_source(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("mcp") / "claude.json"
    path.write_text(json.dumps({"mcpServers": {"nomad-pm": FAKE_PM,
                                               "penpot": {"type": "http", "url": "http://x"}}}))
    monkeypatch.setenv("NOMAD_AUTOPILOT_MCP_SOURCE", str(path))
    return path
