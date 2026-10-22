"""scripts/mcp-registry.py: the plugin MCPs are listed, and every server has a tier (#1412)."""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("mcp_registry", os.path.join(HERE, "mcp-registry.py"))
reg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reg)


def test_plugin_servers_are_read_from_the_repo():
    assert {"nomad-pm", "nomad-comms", "nomad-research", "nomad-content", "nomad-dev"} <= set(reg.plugin_servers())


def test_every_plugin_server_has_a_tier():
    missing = [n for n in reg.plugin_servers() if n not in reg.META]
    assert not missing, missing


def test_render_lists_the_lane_mcps():
    text = reg.render(reg.plugin_servers())
    for lane in ("comms", "research", "content", "dev"):
        assert f"| `nomad-{lane}` |" in text
