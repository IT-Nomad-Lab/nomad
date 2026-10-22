"""The nomad-operator plugin's tier-gate hook must cover every tool the old install.sh hook
covered (#1409): sync-settings retires that entry, so a tool missing here goes unchecked. The
installer no longer wires hooks itself (#1054), so its last matcher is pinned here."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_INSTALL_SH = "Bash|Read|Grep|Glob|Edit|Write|NotebookEdit|WebFetch|mcp__.*"


def tools(matcher):
    return set(matcher.split("|"))


def plugin_matcher():
    with open(os.path.join(ROOT, "nomad-operator", "hooks", "hooks.json")) as f:
        pre = json.load(f)["hooks"]["PreToolUse"]
    return [g["matcher"] for g in pre
            if any("nomad-run" in h["command"] and "tier-gate" in h["command"] for h in g["hooks"])]


def test_plugin_gate_covers_old_install_sh():
    (plugin,) = plugin_matcher()
    assert tools(OLD_INSTALL_SH) <= tools(plugin)
    assert "PowerShell" in tools(plugin)
