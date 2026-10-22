"""Host-native services must default to a loopback bind (#821).

On 0.0.0.0 they answered in plain HTTP on the tailnet IP, outside `tailscale serve`. Under Docker
Desktop, `host.docker.internal` landed on the host loopback. Under Docker Engine (#383) it lands
on the bridge gateway, so the services containers call also listen on exactly that one address,
BRIDGE_IP, named by NOMAD_BRIDGE_IP in their units. Never 0.0.0.0. The services pull in GPU and
web deps, so this reads their source rather than importing them.

Run: python3 -m pytest dispatcher/test_binds.py -q
"""
import os
import re
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest
import yaml

import bridge_bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_IP = "172.17.0.1"   # docs/docker-engine/daemon.json "bip" and "host-gateway-ip"

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


UNITS = ["nomad-image/nomad-comfyui.service", "nomad-image/nomad-comfyui.engine.conf",
         "nomad-audio/nomad-audio.service", "nomad-voice/nomad-voice.service",
         "nomad-stt/nomad-stt.service", "codex-bridge/nomad-codex-bridge.service",
         "dispatcher/nomad-dispatch.service", "autopilot/nomad-autopilot.service",
         "nomad-detect/nomad-detect.service", "ollama-relay/nomad-ollama-relay.service"]


def _body(rel):
    return "\n".join(l for l in _read(rel).splitlines() if not l.lstrip().startswith("#"))


@pytest.mark.parametrize("rel", UNITS)
def test_units_do_not_widen_the_bind(rel):
    assert "0.0.0.0" not in _body(rel), f"{rel} sets a 0.0.0.0 bind"


# ── Docker Engine (#383): loopback plus exactly the bridge gateway ──────────────────────────────

# Services a container calls through host.docker.internal, and the unit that starts each one.
BRIDGED = [
    ("claude-bridge/bridge.py", None),   # unit lives on the host only; see the runbook
    ("codex-bridge/bridge.py", "codex-bridge/nomad-codex-bridge.service"),
    ("dispatcher/dispatch.py", "dispatcher/nomad-dispatch.service"),
    ("dispatcher/termd.py", None),       # unit lives on the host only; see the runbook
    ("autopilot/supervisor.py", "autopilot/nomad-autopilot.service"),
    ("nomad-voice/server.py", "nomad-voice/nomad-voice.service"),
    ("nomad-audio/server.py", "nomad-audio/nomad-audio.service"),
    ("nomad-detect/server.py", "nomad-detect/nomad-detect.service"),
]


@pytest.mark.parametrize("rel,unit", BRIDGED)
def test_bridged_service_listens_through_bridge_bind(rel, unit):
    src = _read(rel)
    assert "bridge_bind" in src, f"{rel} doesn't bind through dispatcher/bridge_bind.py"
    assert not re.search(r"ThreadingHTTPServer\(\(HOST|uvicorn\.run\(", src), \
        f"{rel} still has a single-address bind"


@pytest.mark.parametrize("unit", sorted({u for _, u in BRIDGED if u} | {"ollama-relay/nomad-ollama-relay.service"}))
def test_unit_names_exactly_the_bridge_ip(unit):
    found = re.findall(r"^Environment=NOMAD_BRIDGE_IP=(.*)$", _body(unit), re.M)
    assert found == [BRIDGE_IP], f"{unit}: NOMAD_BRIDGE_IP={found}"


def test_comfyui_engine_dropin_adds_only_the_bridge():
    exec_start = [l for l in _body("nomad-image/nomad-comfyui.engine.conf").splitlines()
                  if l.startswith("ExecStart=") and l != "ExecStart="]
    assert len(exec_start) == 1
    assert f"--listen 127.0.0.1,{BRIDGE_IP} " in exec_start[0]


def test_daemon_json_pins_the_bridge():
    import json
    cfg = json.loads(_read("docs/docker-engine/daemon.json"))
    assert cfg["bip"].split("/")[0] == BRIDGE_IP
    assert cfg["host-gateway-ip"] == BRIDGE_IP


@pytest.mark.parametrize("rel", ["docker-compose.yml", "n8n/docker-compose.yml"])
def test_every_compose_service_maps_host_docker_internal(rel):
    services = yaml.safe_load(_read(rel))["services"]
    for name, svc in services.items():
        assert "host.docker.internal:${NOMAD_HOST_GATEWAY:-host-gateway}" in svc.get("extra_hosts", []), \
            f"{rel}: {name} has no host.docker.internal mapping"


@pytest.mark.parametrize("rel", ["docker-compose.engine.yml", "n8n/docker-compose.engine.yml",
                                 "zulip/compose.engine.yaml"])
def test_engine_overrides_publish_only_on_the_bridge(rel):
    for name, svc in yaml.safe_load(_read(rel))["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith(BRIDGE_IP + ":"), f"{rel}: {name} publishes {port}"


@pytest.mark.parametrize("base,override", [("docker-compose.yml", "docker-compose.engine.yml"),
                                            ("n8n/docker-compose.yml", "n8n/docker-compose.engine.yml"),
                                            ("zulip/compose.yaml", "zulip/compose.engine.yaml")])
def test_engine_overrides_name_only_real_services(base, override):
    # An override naming a service the base file dropped makes compose build a broken service.
    services = yaml.safe_load(_read(base))["services"]
    for name in yaml.safe_load(_read(override))["services"]:
        assert name in services, f"{override}: {name} isn't a service in {base}"


def test_addresses_default_to_loopback_only(monkeypatch):
    monkeypatch.delenv(bridge_bind.BRIDGE_ENV, raising=False)
    assert bridge_bind.addresses("127.0.0.1") == ["127.0.0.1"]
    monkeypatch.setenv(bridge_bind.BRIDGE_ENV, BRIDGE_IP)
    assert bridge_bind.addresses("127.0.0.1") == ["127.0.0.1", BRIDGE_IP]
    monkeypatch.setenv(bridge_bind.BRIDGE_ENV, "127.0.0.1")
    assert bridge_bind.addresses("127.0.0.1") == ["127.0.0.1"]


@pytest.mark.parametrize("wide", ["0.0.0.0", "::"])
def test_bridge_bind_refuses_every_interface(monkeypatch, wide):
    monkeypatch.setenv(bridge_bind.BRIDGE_ENV, wide)
    with pytest.raises(ValueError):
        bridge_bind.addresses("127.0.0.1")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_servers_bind_the_bridge_before_docker0_exists(monkeypatch):
    """IP_FREEBIND: the bridge bind succeeds whether or not 172.17.0.1 is on an interface."""
    class Ok(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    monkeypatch.setenv(bridge_bind.BRIDGE_ENV, BRIDGE_IP)
    port = _free_port()
    servers = bridge_bind.http_servers("127.0.0.1", port, Ok)
    try:
        assert [s.server_address for s in servers] == [("127.0.0.1", port), (BRIDGE_IP, port)]
        threading.Thread(target=servers[0].serve_forever, daemon=True).start()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            assert r.read() == b"ok"
    finally:
        servers[0].shutdown()
        for s in servers:
            s.server_close()


def test_listen_sockets_one_per_address(monkeypatch):
    monkeypatch.setenv(bridge_bind.BRIDGE_ENV, BRIDGE_IP)
    port = _free_port()
    socks = bridge_bind.listen_sockets("127.0.0.1", port)
    try:
        assert [s.getsockname() for s in socks] == [("127.0.0.1", port), (BRIDGE_IP, port)]
    finally:
        for s in socks:
            s.close()
