#!/usr/bin/env python3
"""Google MCP proxy: Gmail, Drive and Calendar MCP with a token the vault keeps fresh (#1294).

Callers (the LiteLLM gateway, or a client) send `Authorization: Bearer <MCP_WRAPPER_SECRET>`. The
proxy reads the OAuth client and refresh token from OpenBao (secret/google/local) through its own
AppRole, exchanges the refresh token for an access token when it has under 5 minutes left, and
forwards the MCP request to https://<svc>mcp.googleapis.com/mcp/v1 with that token. One 401 from
Google forces one refresh and one retry. No token is logged.

  /gmail/mcp  /drive/mcp  /calendar/mcp   (anything after /mcp is passed on)

  google_proxy.py --listen 172.17.0.1:8312
"""
import argparse
import asyncio
import hmac
import json
import os
import time

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from auth_proxy import HOP, secret

SERVICES = {"gmail", "drive", "calendar"}
TOKEN_URL = "https://oauth2.googleapis.com/token"
SKEW = 300


class Unavailable(Exception):
    pass


class Tokens:
    """Vault login + Google access token, both cached, one refresh at a time."""

    def __init__(self, client: httpx.AsyncClient, vault: str, approle: str, path: str):
        self.c, self.vault, self.approle_file, self.path = client, vault.rstrip("/"), approle, path
        self._vault_token, self._vault_exp = "", 0.0
        self._access, self._access_exp = "", 0.0
        self._lock = asyncio.Lock()

    async def _login(self) -> str:
        if self._vault_token and time.time() < self._vault_exp - 60:
            return self._vault_token
        try:
            ar = json.load(open(self.approle_file))
            r = await self.c.post(f"{self.vault}/v1/auth/approle/login", json=ar)
        except (OSError, ValueError, httpx.HTTPError) as e:
            raise Unavailable(f"vault login: {type(e).__name__}") from None
        if r.status_code != 200:
            raise Unavailable(f"vault login: HTTP {r.status_code}")
        a = r.json()["auth"]
        self._vault_token, self._vault_exp = a["client_token"], time.time() + int(a.get("lease_duration", 3600))
        return self._vault_token

    async def _refresh(self) -> None:
        vt = await self._login()
        try:
            r = await self.c.get(f"{self.vault}/v1/{self.path}", headers={"X-Vault-Token": vt})
        except httpx.HTTPError as e:
            raise Unavailable(f"vault read: {type(e).__name__}") from None
        if r.status_code != 200:
            self._vault_token = ""          # token revoked or sealed: log in again next time
            raise Unavailable(f"vault read: HTTP {r.status_code}")
        d = r.json()["data"]["data"]
        try:
            g = await self.c.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                "client_id": d["client_id"], "client_secret": d["client_secret"]})
        except httpx.HTTPError as e:
            raise Unavailable(f"google token: {type(e).__name__}") from None
        if g.status_code != 200:
            raise Unavailable(f"google token: HTTP {g.status_code} {g.json().get('error', '')}")
        j = g.json()
        self._access, self._access_exp = j["access_token"], time.time() + int(j.get("expires_in", 3600))

    async def get(self, force: bool = False) -> str:
        async with self._lock:
            if force or not self._access or time.time() > self._access_exp - SKEW:
                await self._refresh()
            return self._access


def build(key: str, vault: str, approle: str, path: str = "secret/data/google/local",
          transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    client = httpx.AsyncClient(timeout=None, transport=transport)
    tokens = Tokens(client, vault, approle, path)

    async def proxy(request: Request):
        got = request.headers.get("authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {key}".encode()):
            return PlainTextResponse("unauthorized", status_code=401)
        svc = request.path_params["svc"]
        if svc not in SERVICES:
            return PlainTextResponse("not found", status_code=404)
        rest = request.path_params.get("rest", "")
        url = f"https://{svc}mcp.googleapis.com/mcp/v1" + (("/" + rest) if rest else "")
        if request.url.query:
            url += "?" + request.url.query
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP | {"authorization"}}
        r = None
        for attempt in (0, 1):
            try:
                tok = await tokens.get(force=attempt == 1)
            except Unavailable as e:
                return JSONResponse({"error": "google token unavailable", "detail": str(e)}, status_code=503)
            up = client.build_request(request.method, url, headers={**headers, "Authorization": f"Bearer {tok}"}, content=body)
            r = await client.send(up, stream=True)
            if r.status_code != 401 or attempt == 1:
                break
            await r.aclose()
        # raw bytes are forwarded, so Content-Encoding must travel with them
        out = {k: v for k, v in r.headers.items() if k.lower() not in HOP}
        return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out, background=None)

    async def health(request: Request):
        try:
            await tokens.get()
        except Unavailable as e:
            return JSONResponse({"ok": False, "detail": str(e)}, status_code=503)
        return JSONResponse({"ok": True})

    return Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/{svc}/mcp", proxy, methods=["GET", "POST", "DELETE"]),
        Route("/{svc}/mcp/{rest:path}", proxy, methods=["GET", "POST", "DELETE"]),
    ])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="172.17.0.1:8312")
    ap.add_argument("--vault", default=os.environ.get("BAO_ADDR", "http://127.0.0.1:8205"))
    ap.add_argument("--approle", default=os.path.expanduser("~/.config/nomad/openbao/proxy-approle.json"))
    a = ap.parse_args()
    host, port = a.listen.rsplit(":", 1)
    uvicorn.run(build(secret(), a.vault, a.approle), host=host, port=int(port), log_level="warning")
