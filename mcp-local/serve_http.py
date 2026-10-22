#!/usr/bin/env python3
"""Serve one of the nomad_*.py MCP servers over Streamable HTTP (MCP gateway spike, #1265).

  serve_http.py nomad_detect 8301              127.0.0.1:8301/mcp
  serve_http.py nomad_pm 8302 --host 172.17.0.1   reachable from the LiteLLM container

The module's own tools and env are used unchanged; only the transport differs.
"""
import argparse
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ap = argparse.ArgumentParser()
ap.add_argument("module")
ap.add_argument("port", type=int)
ap.add_argument("--host", default="127.0.0.1")
a = ap.parse_args()

mcp = importlib.import_module(a.module).mcp
mcp.settings.host, mcp.settings.port = a.host, a.port
# the SDK rejects any Host header it was not told about (421); the gateway calls by IP
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[f"{a.host}:{a.port}", f"127.0.0.1:{a.port}", f"localhost:{a.port}"])
mcp.run(transport="streamable-http")
