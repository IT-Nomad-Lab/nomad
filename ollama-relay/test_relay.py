"""ollama-relay: gateway lookup, upstream choice and the bind guard (#383).

Run: python3 -m pytest ollama-relay -q
"""
import asyncio
import importlib.util
import os

import pytest

# By path, under its own name: nomad-relay/relay.py is a different module called `relay`.
_spec = importlib.util.spec_from_file_location(
    "ollama_relay", os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay.py"))
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)

ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t0180A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0
eth0\t0080A8C0\t00000000\t0001\t0\t0\t0\t00F0FFFF\t0\t0\t0
"""


def test_default_gateway_reads_little_endian_hex(tmp_path):
    f = tmp_path / "route"
    f.write_text(ROUTE)
    assert relay.default_gateway(str(f)) == "192.168.128.1"


def test_no_default_route_is_none(tmp_path):
    f = tmp_path / "route"
    f.write_text(ROUTE.splitlines()[0] + "\n" + ROUTE.splitlines()[2] + "\n")
    assert relay.default_gateway(str(f)) is None
    assert relay.default_gateway(str(tmp_path / "missing")) is None


def test_pinned_upstream_wins(monkeypatch):
    monkeypatch.setenv("NOMAD_OLLAMA_UPSTREAM", "127.0.0.1:11500")
    assert relay.upstream() == ("127.0.0.1", 11500)
    monkeypatch.setenv("NOMAD_OLLAMA_UPSTREAM", "10.0.0.2")
    assert relay.upstream() == ("10.0.0.2", 11434)


def test_gateway_upstream(monkeypatch):
    monkeypatch.delenv("NOMAD_OLLAMA_UPSTREAM", raising=False)
    monkeypatch.setattr(relay, "default_gateway", lambda: "172.21.128.1")
    assert relay.upstream() == ("172.21.128.1", 11434)
    monkeypatch.setattr(relay, "default_gateway", lambda: None)
    with pytest.raises(OSError):
        relay.upstream()


@pytest.mark.parametrize("addr", ["", "0.0.0.0", "127.0.0.1"])
def test_refuses_wide_or_loopback_bind(monkeypatch, addr):
    monkeypatch.setenv("NOMAD_BRIDGE_IP", addr)
    with pytest.raises(SystemExit):
        asyncio.run(relay.main())


def test_pipes_bytes_to_upstream(monkeypatch):
    async def go():
        async def echo(r, w):
            w.write(b"up:" + await r.read(100))
            await w.drain()
            w.close()
        up = await asyncio.start_server(echo, "127.0.0.1", 0)
        monkeypatch.setenv("NOMAD_OLLAMA_UPSTREAM", f"127.0.0.1:{up.sockets[0].getsockname()[1]}")
        front = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", front.sockets[0].getsockname()[1])
        w.write(b"ping")
        await w.drain()
        w.write_eof()
        got = await asyncio.wait_for(r.read(100), 5)
        w.close()
        front.close()
        up.close()
        return got
    assert asyncio.run(go()) == b"up:ping"
