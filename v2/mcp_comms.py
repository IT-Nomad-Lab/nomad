"""NOMAD v2 · P1-4 — the one MCP tool: `comms.send_message`.

A FastMCP server exposing the single side-effecting action of the Phase-1 slice. The Comms
specialist (P1-5) calls it over MCP; the engine's Execute step calls the same implementation
(`send_message_impl`) so the gated action always goes through one code path.

Phase-1 "send" = deliver to the local `outbox` table (observable, verifiable, reversible). A
real channel (email via the existing Gmail action) is a Phase-2 swap behind the same tool.

Run as an MCP server (stdio):  python3 v2/mcp_comms.py
"""
import datetime
import json
import os
import re
import urllib.request

from mcp.server.fastmcp import FastMCP
from nocodb import NocoDB, _env

_E = _env()
_db = NocoDB()
mcp = FastMCP("nomad-comms")

N8N_SEND_URL = _E.get("N8N_V2_SEND_URL", "http://localhost:5678/webhook/v2-send")
OPERATOR_EMAIL = _E.get("OPERATOR_EMAIL", "")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _email(to: str) -> str:
    """Send to a real address; non-emails (e.g. 'operator') fall back to the operator."""
    return to if _EMAIL.match((to or "").strip()) else OPERATOR_EMAIL


def _run_approved(run_id: str) -> bool:
    """App-layer interlock: only send for a run the operator approved (defense in depth)."""
    row = _db.find("comms", "run_id", run_id)
    return bool(row and (row.get("status") in ("approved", "executing", "executed")))


def send_message_impl(to: str, body: str, run_id: str, subject: str = "Message from NOMAD") -> dict:
    """Send a real email (via the n8n Gmail action) + record to the outbox audit table.
    REFUSES unless the run is approved — so the tool itself can't be used to bypass the gate."""
    if not _run_approved(run_id):
        return {"ok": False, "gated": True, "reason": "run not approved — refusing to send"}
    addr = _email(to)
    sent = False
    if addr:
        try:
            req = urllib.request.Request(
                N8N_SEND_URL, method="POST", headers={"Content-Type": "application/json"},
                data=json.dumps({"to": addr, "subject": subject, "body": body}).encode())
            urllib.request.urlopen(req, timeout=20)
            sent = True
        except Exception:
            sent = False
    out = _db.create("outbox", {"run_id": run_id, "to": addr or to,
                                "body": f"Subject: {subject}\n\n{body}", "delivered_at": _now()})
    return {"ok": True, "outbox_id": out["Id"], "sent": sent, "to": addr or to}


@mcp.tool()
def send_message(to: str, body: str, run_id: str, subject: str = "Message from NOMAD") -> dict:
    """Send a message (real email via Gmail + outbox audit) to `to` for `run_id`. Returns
    {ok, outbox_id, sent}. Runs only after the human gate approves; refuses otherwise."""
    return send_message_impl(to, body, run_id, subject)


if __name__ == "__main__":
    mcp.run()
