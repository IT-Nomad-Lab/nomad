"""Windows Ollama for containers on Docker Engine (#383).

Ollama runs natively on Windows. Under Docker Desktop, containers reached it as
host.docker.internal:11434. Under Docker Engine in WSL that name is the bridge gateway
(NOMAD_BRIDGE_IP, 172.17.0.1), which is this distro, not Windows. This relay listens there and
pipes each connection to Windows through the WSL NAT gateway: the default route's gateway in
/proc/net/route. It looks the gateway up per connection, because WSL picks a new NAT subnet
after a Windows reboot, so an address baked into compose or .env would go stale.

Listens on NOMAD_BRIDGE_IP only, never loopback or 0.0.0.0, so it can't take 127.0.0.1:11434 from a
WSL-side Ollama, and nothing reaches it from the LAN. The bind uses IP_FREEBIND, so it starts
before docker0 exists.

Env: NOMAD_BRIDGE_IP (required), NOMAD_OLLAMA_RELAY_PORT (11434),
     NOMAD_OLLAMA_UPSTREAM (host:port, overrides the gateway lookup, e.g. for mirrored mode).
Run: python3 ollama-relay/relay.py      Test: python3 -m pytest ollama-relay -q
"""
import asyncio
import os
import socket
import struct
import sys

PORT = int(os.environ.get("NOMAD_OLLAMA_RELAY_PORT", "11434"))
UPSTREAM_PORT = 11434
_IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)


def default_gateway(route_file="/proc/net/route"):
    """The IPv4 default route's gateway, or None. The kernel writes it as little-endian hex."""
    try:
        with open(route_file) as f:
            next(f)   # header
            for line in f:
                fields = line.split()
                if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, StopIteration, ValueError):
        pass
    return None


def upstream():
    """(host, port) of Windows Ollama right now."""
    pinned = os.environ.get("NOMAD_OLLAMA_UPSTREAM", "").strip()
    if pinned:
        host, _, port = pinned.rpartition(":")
        return (host, int(port)) if host else (pinned, UPSTREAM_PORT)
    gw = default_gateway()
    if not gw:
        raise OSError("no default route, so no WSL NAT gateway to reach Windows through")
    return gw, UPSTREAM_PORT


async def _pipe(reader, writer):
    """Copy one direction; pass EOF on as a half-close so the reply can still come back."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except OSError:
        pass


async def handle(client_r, client_w):
    try:
        host, port = upstream()
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
    except (OSError, asyncio.TimeoutError) as e:
        print(f"ollama-relay: upstream unreachable: {e}", file=sys.stderr, flush=True)
        client_w.close()
        return
    try:
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))
    finally:
        up_w.close()
        client_w.close()


def listen_socket(addr, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, _IP_FREEBIND, 1)
    s.bind((addr, port))
    return s


async def main():
    addr = os.environ.get("NOMAD_BRIDGE_IP", "").strip()
    if not addr or addr in ("0.0.0.0", "127.0.0.1"):
        sys.exit("ollama-relay: set NOMAD_BRIDGE_IP to the Docker bridge gateway (not 0.0.0.0 or loopback)")
    server = await asyncio.start_server(handle, sock=listen_socket(addr, PORT))
    print(f"ollama-relay on {addr}:{PORT} -> {os.environ.get('NOMAD_OLLAMA_UPSTREAM') or 'WSL NAT gateway'}"
          f":{UPSTREAM_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
