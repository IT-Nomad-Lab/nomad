#!/usr/bin/env python3
"""Bearer-token reverse proxy in front of a host MCP wrapper.

The wrapper listens on 127.0.0.1 only; this listens where the gateway container can reach it
(172.17.0.1) and forwards a request only when `Authorization: Bearer <secret>` matches
MCP_WRAPPER_SECRET (from ~/.config/nomad/mcp-gateway.env). Streams, so SSE responses work.

  auth_proxy.py --listen 172.17.0.1:8310 --upstream http://127.0.0.1:18310
"""
import argparse
import hmac
import os
import re

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route

HOP = {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "upgrade"}


def secret() -> str:
    s = os.environ.get("MCP_WRAPPER_SECRET", "")
    if not s:
        m = re.search(r"(?m)^MCP_WRAPPER_SECRET=(.+)$", open(os.path.expanduser("~/.config/nomad/mcp-gateway.env")).read())
        s = m.group(1).strip() if m else ""
    if len(s) < 24:
        raise SystemExit("MCP_WRAPPER_SECRET missing or too short")
    return s


def build(upstream: str, key: str) -> Starlette:
    client = httpx.AsyncClient(base_url=upstream, timeout=None)

    async def proxy(request: Request):
        got = request.headers.get("authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {key}".encode()):
            return PlainTextResponse("unauthorized", status_code=401)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP | {"authorization"}}
        up = client.build_request(request.method, request.url.path + ("?" + request.url.query if request.url.query else ""),
                                  headers=headers, content=await request.body())
        r = await client.send(up, stream=True)
        out = {k: v for k, v in r.headers.items() if k.lower() not in HOP | {"content-encoding"}}
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out, background=None)

    return Starlette(routes=[Route("/{path:path}", proxy, methods=["GET", "POST", "DELETE", "OPTIONS"])])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", required=True)
    ap.add_argument("--upstream", required=True)
    a = ap.parse_args()
    host, port = a.listen.rsplit(":", 1)
    uvicorn.run(build(a.upstream.rstrip("/"), secret()), host=host, port=int(port), log_level="warning")
