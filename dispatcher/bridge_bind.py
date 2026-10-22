"""Listen on loopback, plus the Docker bridge gateway when NOMAD_BRIDGE_IP names it (#383).

Under Docker Desktop, `host.docker.internal` landed on the host loopback, so a 127.0.0.1 bind was
enough (#821). Under Docker Engine in WSL it resolves to the bridge gateway instead, 172.17.0.1, so
a service containers call must listen there too. Never 0.0.0.0: WSL NAT doesn't expose the bridge
to the LAN, and the tailnet only sees what `tailscale serve` forwards.

The bridge socket is bound with IP_FREEBIND, so the service starts whether or not docker0 exists
yet. That covers boot order (a user unit can start before the system docker.service) and the time
before the migration, when Docker Desktop is still the daemon and there's no docker0 at all.

Stdlib only. Services outside dispatcher/ load it by path, like autopilot/projects.py loads
projects_registry. See docs/DOCKER-ENGINE-RUNBOOK.md.
"""
import os
import socket
import threading
from http.server import ThreadingHTTPServer

BRIDGE_ENV = "NOMAD_BRIDGE_IP"
_IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)   # Linux value; older Pythons lack the name


def addresses(host):
    """`host` first, then the bridge address if one is set and differs."""
    bridge = os.environ.get(BRIDGE_ENV, "").strip()
    if bridge in ("0.0.0.0", "::"):
        raise ValueError(f"{BRIDGE_ENV}={bridge} would listen on every interface; name the bridge gateway")
    return [host] + ([bridge] if bridge and bridge != host else [])


def listen_socket(addr, port, backlog=2048):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if addr != "127.0.0.1":
        s.setsockopt(socket.IPPROTO_IP, _IP_FREEBIND, 1)
    s.bind((addr, port))
    s.listen(backlog)
    return s


def listen_sockets(host, port):
    """One listening socket per address, for uvicorn.Server.run(sockets=...) or websockets."""
    return [listen_socket(a, port) for a in addresses(host)]


def http_servers(host, port, handler, server_class=ThreadingHTTPServer):
    """One http.server per address. Serve the first in the foreground; start_extra() the rest."""
    servers = []
    for addr in addresses(host):
        srv = server_class((addr, port), handler, bind_and_activate=False)
        if addr != "127.0.0.1":
            srv.socket.setsockopt(socket.IPPROTO_IP, _IP_FREEBIND, 1)
        try:
            srv.server_bind()
            srv.server_activate()
        except Exception:
            srv.server_close()
            for s in servers:
                s.server_close()
            raise
        servers.append(srv)
    return servers


def start_extra(servers):
    """serve_forever every server but the first, each on a daemon thread."""
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True,
                         name=f"bridge-{srv.server_address[0]}").start()


def serve_uvicorn(app, host, port, **config):
    """Blocking: uvicorn.run(), but on one socket per address."""
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, **config))
    server.run(sockets=listen_sockets(host, port))


def serve_http(host, port, handler):
    """Blocking: the loopback server in the foreground, the bridge one on a thread."""
    servers = http_servers(host, port, handler)
    start_extra(servers)
    servers[0].serve_forever()
