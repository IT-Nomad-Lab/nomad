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
# n8n publishes 5678 on 127.0.0.1, and the send workflow is webhook straight into Gmail Send:
# the approval interlock below lives here, in front of n8n, not inside the workflow. So the
# webhook itself requires this shared header (n8n credential "NOMAD webhook token", 2026-09-23).
N8N_WEBHOOK_TOKEN = _E.get("N8N_WEBHOOK_TOKEN", "")
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


# Outbox `status` values, in descending order of how much they claim.
#
# `sent` is written only when Gmail answered with its own message id. The v2-send webhook used to
# run with responseMode `onReceived`, so it replied BEFORE its Gmail node and a 200 proved only
# that n8n took the request; `accepted` was the most this side of the wire could honestly claim.
# It responds from its last node now, so the reply carries the id and `sent` is a fact.
#
# `accepted` stays, and is not dead code. It is what a 200 with no message id means, which is
# exactly what an n8n that has not been restarted onto the new workflow still returns. The engine
# must not start claiming `sent` because a workflow somewhere was supposed to have been upgraded.
#
# `unknown` exists because the fix created a failure the old shape could not have. Waiting for
# Gmail means the request can now time out AFTER the mail left. Calling that `failed` would be the
# same kind of lie in the opposite direction, and the operator would retry and send it twice.
# `ok` is still False for it, so the run does not read as executed: a run sitting at failed is
# recoverable and needs a fresh approval, a run wrongly marked executed is not.
SENT, ACCEPTED, UNKNOWN, FAILED = "sent", "accepted", "unknown", "failed"

# The webhook now waits for Gmail, so the old 20s is the send's budget rather than n8n's.
SEND_TIMEOUT = int(_E.get("NOMAD_SEND_TIMEOUT", "45"))

# Columns added after the table was created. A base that predates either migration must still
# record the attempt, because losing the row is worse than losing the detail.
_NEW_COLUMNS = ("status", "error", "provider_id")


def _provider_id(raw: bytes) -> str:
    """Gmail's own message id out of the webhook reply, or "" when there is not one.

    With responseMode `lastNode` the reply is the Gmail node's output and carries `id`. Empty is
    the honest answer for everything else, including the acknowledgement an un-upgraded
    `onReceived` webhook still sends, and the caller degrades to `accepted` rather than guessing."""
    try:
        d = json.loads(raw or b"")
    except Exception:
        return ""
    if isinstance(d, list):
        d = d[0] if d and isinstance(d[0], dict) else {}
    return str(d.get("id") or "")[:200] if isinstance(d, dict) else ""


def _timed_out(e: Exception) -> bool:
    """A read timeout arrives as TimeoutError; a connect timeout as URLError wrapping one."""
    return isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)


def send_message_impl(to: str, body: str, run_id: str, subject: str = "Message from NOMAD") -> dict:
    """Send a real email (via the n8n Gmail action) + record to the outbox audit table.
    REFUSES unless the run is approved — so the tool itself can't be used to bypass the gate.

    A send that did not go through fails the run. It used to return ok=True whatever happened, and
    stamp `delivered_at` on the row either way, so a refused webhook and a delivered email looked
    identical in the audit table and the run still read as `executed`. That is the same shape as the
    n8n gate reporting success for an action that never ran. Failing here is recoverable: the
    operator retries, which puts the run back at the gate and needs a fresh approval, so nothing
    re-sends behind their back.
    """
    if not _run_approved(run_id):
        return {"ok": False, "gated": True, "reason": "run not approved — refusing to send"}
    addr = _email(to)
    status, provider_id = FAILED, ""
    err = "" if addr else "no address"
    if addr:
        headers = {"Content-Type": "application/json"}
        if N8N_WEBHOOK_TOKEN:
            headers["X-Nomad-Token"] = N8N_WEBHOOK_TOKEN
        try:
            req = urllib.request.Request(
                N8N_SEND_URL, method="POST", headers=headers,
                data=json.dumps({"to": addr, "subject": subject, "body": body}).encode())
            with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as resp:
                provider_id = _provider_id(resp.read())
            # An id is Gmail's own receipt. Without one all we know is that n8n took the request.
            status = SENT if provider_id else ACCEPTED
        except Exception as e:  # a 403 here means the webhook token is missing or wrong
            err = f"{type(e).__name__}: {e}"
            if _timed_out(e):
                status = UNKNOWN
                err = (f"{err} — timed out waiting for Gmail. The message may have gone out; "
                       "check the mailbox before approving a retry.")
    ok = status in (SENT, ACCEPTED)
    # The row is written either way, because a failed attempt is exactly what an audit table is for.
    # `delivered_at` is left empty unless the send is claimed: a timestamp says something happened.
    row = {"run_id": run_id, "to": addr or to, "body": f"Subject: {subject}\n\n{body}",
           "status": status, "error": "" if ok else err[:500], "provider_id": provider_id,
           "delivered_at": _now() if ok else None}
    try:
        out = _db.create("outbox", row)
    except Exception:
        # An older base predates these columns. Record the attempt rather than losing it, and let
        # the returned error carry the outcome: a missing column must not turn a failed send into a
        # silent one, which is the whole point of this function.
        out = _db.create("outbox", {k: v for k, v in row.items() if k not in _NEW_COLUMNS})
    r = {"ok": ok, "outbox_id": out["Id"], "sent": ok, "status": status, "to": addr or to}
    if provider_id:
        r["provider_id"] = provider_id
    if not ok:
        r["error"] = err
    return r


@mcp.tool()
def send_message(to: str, body: str, run_id: str, subject: str = "Message from NOMAD") -> dict:
    """Send a message (real email via Gmail + outbox audit) to `to` for `run_id`. Returns
    {ok, outbox_id, sent}. Runs only after the human gate approves; refuses otherwise."""
    return send_message_impl(to, body, run_id, subject)


if __name__ == "__main__":
    mcp.run()
