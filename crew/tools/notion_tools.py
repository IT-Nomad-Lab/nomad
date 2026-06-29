"""Shared-memory + approval-gate tools for the crew.

These are the ONLY way agents touch mission control. Reversible writes
(tasks, activity log, knowledge) happen freely. Irreversible actions never
execute here — `request_approval` just files a Pending row and returns; n8n's
wait-for-approval workflow performs the action only after you tap Approve.
"""
import os
import re
from datetime import datetime, timezone

import requests
from crewai.tools import tool
from notion_client import Client

# Uses the notion-client default (2025-09 data-source) API. The 7 databases'
# data sources already carry the expected columns (set up out-of-band), so
# page creates via {"database_id": ...} resolve and validate correctly.
_notion = Client(auth=os.environ["NOTION_TOKEN"]) if os.environ.get("NOTION_TOKEN") else None

DB_TASKS = os.environ.get("NOTION_DB_TASKS")
DB_ACTIVITY = os.environ.get("NOTION_DB_ACTIVITY")
DB_APPROVALS = os.environ.get("NOTION_DB_APPROVALS")
DB_KNOWLEDGE = os.environ.get("NOTION_DB_KNOWLEDGE")
APPROVAL_WEBHOOK = os.environ.get("N8N_APPROVAL_WEBHOOK")

# Action types that must NEVER be executed by an agent directly.
GATED_TYPES = {
    "Send Email", "Merge to main", "External Invite",
    "Share File", "Publish", "Delete", "Spend",
}


_ds_cache = {}


def _ds_id(db_id):
    """Resolve a database's primary data-source id (2025-09 API), cached."""
    if db_id not in _ds_cache:
        _ds_cache[db_id] = _notion.databases.retrieve(db_id)["data_sources"][0]["id"]
    return _ds_cache[db_id]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _title(text):
    return {"title": [{"text": {"content": text[:200]}}]}


def _rich(text):
    return {"rich_text": [{"text": {"content": text[:1900]}}]}


# ── plain (non-tool) writers — callable directly by orchestration code so logging
#    is DETERMINISTIC and never depends on an LLM successfully invoking a tool. ──
def write_activity(agent: str, action: str, detail: str = "") -> bool:
    if not (_notion and DB_ACTIVITY):
        return False
    try:
        _notion.pages.create(parent={"database_id": DB_ACTIVITY}, properties={
            "Action": _title(action),
            "Agent": {"select": {"name": agent}},
            "Timestamp": {"date": {"start": _now()}},
            "Detail": _rich(detail),
        })
        return True
    except Exception:
        return False


def fetch_backlog(agent: str = "Engineering Crew", limit: int = 10) -> list:
    """Return queued self-dev tasks: Tasks rows with Status=Backlog + the given agent.
    Each item: {id, title}. The project is encoded as a [Project] prefix in the title."""
    if not (_notion and DB_TASKS):
        return []
    try:
        res = _notion.data_sources.query(
            data_source_id=_ds_id(DB_TASKS),
            filter={"and": [
                {"property": "Status", "select": {"equals": "Backlog"}},
                {"property": "Assigned Agent", "select": {"equals": agent}},
            ]},
            page_size=limit,
        )
    except Exception:
        return []
    out = []
    for r in res.get("results", []):
        title = ""
        for v in r["properties"].values():
            if v.get("type") == "title":
                title = "".join(t["plain_text"] for t in v["title"])
                break
        out.append({"id": r["id"], "title": title})
    return out


def set_task_status_by_id(page_id: str, status: str) -> bool:
    """Set a Task row's Status by page id (the self-dev loop knows ids, not just titles)."""
    if not _notion:
        return False
    try:
        _notion.pages.update(page_id, properties={"Status": {"select": {"name": status}}})
        return True
    except Exception:
        return False


def write_knowledge(title: str, body: str = "", kind: str = "Brief", source: str = "") -> bool:
    if not (_notion and DB_KNOWLEDGE):
        return False
    props = {"Title": _title(title), "Type": {"select": {"name": kind}}}
    if source:
        # Source is a Notion URL property — only set it when source is a real URL.
        # A non-URL (e.g. a file path) is rejected by the API, so fold it into the body.
        if re.match(r"https?://", source):
            props["Source"] = {"url": source}
        else:
            body = f"Source: {source}\n\n{body}" if body else f"Source: {source}"
    chunks = [body[i:i + 1900] for i in range(0, len(body), 1900)] if body else []
    children = [{"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": [{"text": {"content": c}}]}} for c in chunks[:12]]
    try:
        _notion.pages.create(parent={"database_id": DB_KNOWLEDGE}, properties=props,
                             children=children or None)
        return True
    except Exception:
        return False


@tool("log_activity")
def log_activity(agent: str, action: str, detail: str = "") -> str:
    """Append a timestamped row to the Notion Agent Activity Log.
    Call this after every meaningful action for the audit trail."""
    if not (_notion and DB_ACTIVITY):
        return "Activity log not configured (NOTION_DB_ACTIVITY missing)."
    return "Logged." if write_activity(agent, action, detail) else "Activity log write failed."


@tool("create_task")
def create_task(title: str, agent: str, status: str = "Backlog") -> str:
    """Create a row in the Notion Tasks database and return its id."""
    if not (_notion and DB_TASKS):
        return "Tasks DB not configured (NOTION_DB_TASKS missing)."
    page = _notion.pages.create(parent={"database_id": DB_TASKS}, properties={
        "Title": _title(title),
        "Assigned Agent": {"select": {"name": agent}},
        "Status": {"select": {"name": status}},
    })
    return page["id"]


@tool("update_task_status")
def update_task_status(title: str, status: str) -> str:
    """Set a Notion Task's Status by its title.
    status ∈ Backlog / In Progress / Review / Done / Blocked. Use this to move a
    task through its lifecycle as work proceeds."""
    if not (_notion and DB_TASKS):
        return "Tasks DB not configured (NOTION_DB_TASKS missing)."
    res = _notion.data_sources.query(
        data_source_id=_ds_id(DB_TASKS),
        filter={"property": "Title", "title": {"equals": title[:100]}},
    )
    results = res.get("results", [])
    if not results:
        return f"No task titled '{title}' found — create it first with create_task."
    _notion.pages.update(results[0]["id"],
                         properties={"Status": {"select": {"name": status}}})
    return f"Task '{title}' → {status}."


@tool("save_knowledge")
def save_knowledge(title: str, kind: str = "Brief", source: str = "") -> str:
    """Save a brief/decision/reference to the Notion Knowledge Base."""
    if not (_notion and DB_KNOWLEDGE):
        return "Knowledge DB not configured (NOTION_DB_KNOWLEDGE missing)."
    return "Saved." if write_knowledge(title, "", kind, source) else "Knowledge write failed."


@tool("request_send_email")
def request_send_email(to: str, subject: str, body: str,
                       requested_by: str = "Comms") -> str:
    """GATE. Request approval to SEND AN EXTERNAL EMAIL. Files a Pending row in
    Approvals and pings n8n with structured {to, subject, body}; the email is
    sent by n8n ONLY after you approve. NEVER sends directly. Returns the row id."""
    context = f"To: {to}\nSubject: {subject}\n\n{body}"
    row_id = None
    if _notion and DB_APPROVALS:
        page = _notion.pages.create(parent={"database_id": DB_APPROVALS}, properties={
            "Action": _title(f"Send email to {to}"),
            "Type": {"select": {"name": "Send Email"}},
            "Status": {"select": {"name": "Pending"}},
            "Requested By": {"select": {"name": requested_by}},
            "Context": _rich(context),
            "Requested At": {"date": {"start": _now()}},
        })
        row_id = page["id"]
    if APPROVAL_WEBHOOK:
        try:
            requests.post(APPROVAL_WEBHOOK, json={
                "approval_id": row_id, "type": "Send Email",
                "action": f"Send email to {to}", "requested_by": requested_by,
                "to": to, "subject": subject, "body": body, "context": context,
            }, timeout=10)
        except requests.RequestException as e:
            return f"Approval row created ({row_id}) but webhook ping failed: {e}"
    return (f"EMAIL APPROVAL REQUESTED (id={row_id}). NOT sent — pending your "
            f"approval. Do not retry or attempt another send path.")


def _gate_request(action: str, action_type: str, requested_by: str,
                  context: str, fields: dict):
    """Shared: file a Pending Approvals row + ping n8n with structured `fields`.
    Returns (row_id, note). The action executes only after human approval in n8n."""
    row_id = None
    if _notion and DB_APPROVALS:
        page = _notion.pages.create(parent={"database_id": DB_APPROVALS}, properties={
            "Action": _title(action),
            "Type": {"select": {"name": action_type}},
            "Status": {"select": {"name": "Pending"}},
            "Requested By": {"select": {"name": requested_by}},
            "Context": _rich(context),
            "Requested At": {"date": {"start": _now()}},
        })
        row_id = page["id"]
    if APPROVAL_WEBHOOK:
        try:
            requests.post(APPROVAL_WEBHOOK, json={
                "approval_id": row_id, "action": action, "type": action_type,
                "context": context, "requested_by": requested_by, **fields,
            }, timeout=10)
        except requests.RequestException as e:
            return row_id, f" (webhook ping failed: {e})"
    return row_id, ""


@tool("request_github_merge")
def request_github_merge(owner: str, repo: str, pull_number: str,
                         requested_by: str = "Builder") -> str:
    """GATE. Request approval to MERGE a GitHub pull request into its base branch.
    Files a Pending approval row and pings n8n; the merge runs ONLY after you approve.
    Never merges directly. owner/repo identify the repository; pull_number is the PR #."""
    action = f"Merge {owner}/{repo} PR #{pull_number}"
    ctx = f"Repository: {owner}/{repo}\nPull request: #{pull_number}"
    rid, note = _gate_request(action, "Merge to main", requested_by, ctx,
                              {"owner": owner, "repo": repo, "pull_number": str(pull_number)})
    return (f"MERGE APPROVAL REQUESTED (id={rid}). NOT merged — pending your approval; "
            f"do not work around the gate.{note}")


@tool("request_calendar_event")
def request_calendar_event(summary: str, start: str, end: str,
                           attendees: str = "", requested_by: str = "Comms") -> str:
    """GATE. Request approval to CREATE a calendar event / send invites. start and end are
    ISO datetimes (e.g. 2026-06-10T15:00:00-04:00); attendees is a comma-separated email
    list. Files a Pending approval and pings n8n; the event is created ONLY after approval."""
    action = f"Calendar: {summary}"
    ctx = f"{summary}\nWhen: {start} → {end}\nAttendees: {attendees or '(none)'}"
    rid, note = _gate_request(action, "External Invite", requested_by, ctx,
                              {"summary": summary, "start": start, "end": end, "attendees": attendees})
    return (f"CALENDAR APPROVAL REQUESTED (id={rid}). NOT created — pending your approval.{note}")


@tool("request_drive_share")
def request_drive_share(file_id: str, email: str, role: str = "reader",
                        requested_by: str = "Comms") -> str:
    """GATE. Request approval to SHARE a Google Drive file with someone. role is
    reader / writer / commenter. Files a Pending approval and pings n8n; the file is
    shared ONLY after you approve. Never shares directly."""
    action = f"Share Drive file with {email}"
    ctx = f"File id: {file_id}\nShare with: {email}\nRole: {role}"
    rid, note = _gate_request(action, "Share File", requested_by, ctx,
                              {"file_id": file_id, "email": email, "role": role})
    return (f"DRIVE SHARE APPROVAL REQUESTED (id={rid}). NOT shared — pending your approval.{note}")


@tool("request_approval")
def request_approval(action: str, action_type: str, context: str,
                     requested_by: str) -> str:
    """GATE. File a high-stakes/irreversible action for human approval and STOP.

    Use for: Send Email, Merge to main, External Invite, Share File, Publish,
    Delete, Spend. This NEVER performs the action — it writes a Pending row to
    the Approvals DB and pings n8n. The action runs only after the human taps
    Approve. Returns the approval row id."""
    if action_type not in GATED_TYPES:
        return (f"'{action_type}' is not a gated type. If it is reversible, just "
                f"do it and log_activity. Gated types: {sorted(GATED_TYPES)}")

    row_id = None
    if _notion and DB_APPROVALS:
        page = _notion.pages.create(parent={"database_id": DB_APPROVALS}, properties={
            "Action": _title(action),
            "Type": {"select": {"name": action_type}},
            "Status": {"select": {"name": "Pending"}},
            "Requested By": {"select": {"name": requested_by}},
            "Context": _rich(context),
            "Requested At": {"date": {"start": _now()}},
        })
        row_id = page["id"]

    # Notify the approval workflow (which owns actually executing the action).
    if APPROVAL_WEBHOOK:
        try:
            requests.post(APPROVAL_WEBHOOK, json={
                "approval_id": row_id, "action": action, "type": action_type,
                "context": context, "requested_by": requested_by,
            }, timeout=10)
        except requests.RequestException as e:  # gate still holds; just note it
            return f"Approval row created ({row_id}) but webhook ping failed: {e}"

    return (f"APPROVAL REQUESTED (id={row_id}). Action is PENDING and was NOT "
            f"performed. Wait for human approval; do not retry or work around it.")
