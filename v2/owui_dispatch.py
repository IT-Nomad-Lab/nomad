"""Dispatch a work package to the agent from Open WebUI (#1441, docs/prd/owui-dispatch.md).

The Dispatch panel in owui/nomad_ui.js lists a board's open work and sends one item to the
engine. Sending is `Engine.run_task`, the same start the chat's `build_task` tool makes: the
item is claimed, routed and stops at the operator's gate. Nothing here approves or resumes.

The agent state the panel shows comes from the work package (its status and Run ID) and the
run's row, so it is the same whether the run started here, in a chat or from the cockpit:

| state      | when                                                         |
|------------|--------------------------------------------------------------|
| (none)     | no run has been started for it                               |
| assigned   | a run id is set but the run has no row yet, or it is routing |
| at-gate    | the run waits for the operator                               |
| building   | approved, and the action is running                          |
| approved   | approved and executed; the item is not closed yet            |
| done       | the item is closed                                           |
| declined · failed · withdrawn | the run ended without the action           |
"""
KINDS = ("Task", "Bug", "User story")
OPEN = ("New", "Backlog", "Ready", "In Progress", "Blocked", "Awaiting approval", "Approved",
        "Changes requested", "In Review")
DEFAULT_BOARD = "nomad-work"
LIMIT = 200
RUN_STATES = {"awaiting-approval": "at-gate", "executing": "building", "executed": "approved",
              "declined": "declined", "failed": "failed", "withdrawn": "withdrawn"}


def _t(wp, field):
    return ((wp.get("_links") or {}).get(field) or {}).get("title") or ""


def _parent(wp):
    p = (wp.get("_links") or {}).get("parent") or {}
    return p.get("title") or ""


def agent_state(run_id: str, run_status: str, closed: bool) -> str:
    if closed:
        return "done" if run_id else ""
    if not run_id:
        return ""
    return RUN_STATES.get(run_status or "", "assigned")


def brief(op, wp, run_status) -> dict:
    """One row of the panel. `run_status` is (run id) -> the run's status, or ""."""
    rid = op.run_of(wp) or ""
    status = _t(wp, "status")
    return {"id": wp["id"], "subject": wp.get("subject") or "", "type": _t(wp, "type"),
            "status": status, "parent": _parent(wp), "run_id": rid,
            "agent": agent_state(rid, run_status(rid) if rid else "", op.closed(status))}


def items(op, run_status, board="") -> dict:
    """Open Tasks, Bugs and User stories on `board` (nomad-work for General), newest first.
    Containers are left out: run_task builds one item, never an Epic or Feature."""
    try:
        pid = op.project()["id"]
        wps = op.project_wps(pid)
    except Exception as e:
        return {"ok": False, "board": board or DEFAULT_BOARD, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    rows = [w for w in wps if _t(w, "type") in KINDS and _t(w, "status") in OPEN]
    rows.sort(key=lambda w: w["id"], reverse=True)
    return {"ok": True, "board": board or DEFAULT_BOARD,
            "items": [brief(op, w, run_status) for w in rows[:LIMIT]]}


def status(op, run_status, ids) -> dict:
    """The agent state of each id, for the panel's poll. An id that can't be read is left out."""
    out = []
    for i in ids[:50]:
        try:
            out.append(brief(op, op.get(int(str(i).lstrip("#"))), run_status))
        except Exception:
            continue
    return {"ok": True, "items": out}


def dispatch(op, run_task, run_status, wp_id) -> dict:
    """Start the item's gate run. The answer is the refreshed row, or the engine's refusal."""
    try:
        wid = int(str(wp_id).strip().lstrip("#"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "id must be a work package number"}
    try:
        st = run_task(wid)
    except Exception as e:
        return {"ok": False, "id": wid, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    if not isinstance(st, dict) or st.get("error"):
        return {"ok": False, "id": wid, "error": (st or {}).get("error") or "nothing started"}
    row = status(op, run_status, [wid])["items"]
    return {"ok": True, "id": wid, "run_id": st.get("run_id"), "lane": st.get("lane"),
            "item": row[0] if row else None,
            "note": "Nothing has run. It waits at the gate: approve it in the cockpit, in Zulip, "
                    "or by saying approve in a chat."}
