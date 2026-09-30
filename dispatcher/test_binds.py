"""Host-native services must default to a loopback bind (#821).

On 0.0.0.0 they answered in plain HTTP on the tailnet IP, outside `tailscale serve`. Containers
don't need the wider bind: `host.docker.internal` lands on the host loopback. The services pull in
GPU and web deps, so this reads their source rather than importing them.

Run: python3 -m pytest dispatcher/test_binds.py -q
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (file, env var that overrides the bind) for each service #821 names, bar ComfyUI.
SERVICES = [
    ("claude-bridge/bridge.py", "CLAUDE_BRIDGE_HOST"),   # 8088
    ("codex-bridge/bridge.py", "CODEX_BRIDGE_HOST"),     # 8089
    ("dispatcher/dispatch.py", "NOMAD_DISPATCH_HOST"),   # 8090
    ("dispatcher/termd.py", "NOMAD_TERM_HOST"),          # 8091
    ("nomad-voice/server.py", "NOMAD_VOICE_HOST"),       # 8200
    ("nomad-stt/server.py", "NOMAD_STT_HOST"),           # 8212
    ("nomad-audio/server.py", "NOMAD_AUDIO_HOST"),       # 8220
]


def _read(rel):
    with open(os.path.join(REPO, rel)) as f:
        return f.read()


@pytest.mark.parametrize("rel,var", SERVICES)
def test_service_defaults_to_loopback(rel, var):
    m = re.search(r'os\.environ\.get\(\s*"' + var + r'"\s*,\s*"([^"]+)"\s*\)', _read(rel))
    assert m, f"{rel}: no default bind found for {var}"
    assert m.group(1) == "127.0.0.1", f"{rel}: {var} defaults to {m.group(1)}"


def test_comfyui_unit_listens_on_loopback():   # 8188
    unit = _read("nomad-image/nomad-comfyui.service")
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "--listen 127.0.0.1" in exec_start


@pytest.mark.parametrize("rel", ["nomad-image/nomad-comfyui.service", "nomad-audio/nomad-audio.service",
                                 "nomad-voice/nomad-voice.service", "nomad-stt/nomad-stt.service",
                                 "codex-bridge/nomad-codex-bridge.service",
                                 "dispatcher/nomad-dispatch.service"])
def test_units_do_not_widen_the_bind(rel):
    body = "\n".join(l for l in _read(rel).splitlines() if not l.lstrip().startswith("#"))
    assert "0.0.0.0" not in body, f"{rel} sets a 0.0.0.0 bind"
