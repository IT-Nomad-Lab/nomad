# Notion Mission Control — Database Schema

Notion is the **shared brain** both you and the agents read/write. Seven linked
databases. You live mostly in **Goals + Milestones + Approvals**; the agents live
in **Tasks + Activity Log + Knowledge**.

`notion/setup_notion.py` creates all seven under your `NOTION_PARENT_PAGE_ID`
and prints their IDs to paste into `.env`.

---

## 1. Projects
Top-level container.

| Property | Type | Notes |
|---|---|---|
| Name | title | |
| Status | select | Active / Paused / Done / Archived |
| Owner | rich_text | |
| Goals | relation → Goals | |

## 2. Goals
Your north stars.

| Property | Type | Notes |
|---|---|---|
| Name | title | |
| Description | rich_text | |
| Success Criteria | rich_text | what "done" means — the Reviewer checks against this |
| Priority | select | Low / Medium / High |
| Target Date | date | |
| Project | relation → Projects | |

## 3. Milestones
Checkpoints under a Goal.

| Property | Type | Notes |
|---|---|---|
| Title | title | |
| Due Date | date | |
| Status | select | Not Started / In Progress / Review / Done |
| % Complete | number | |
| Goal | relation → Goals | |

## 4. Tasks
The unit of agent work.

| Property | Type | Notes |
|---|---|---|
| Title | title | |
| Assigned Agent | select | Orchestrator / Planner / Researcher / Builder / Writer / Comms / Reviewer |
| Status | select | Backlog / In Progress / Review / Done / Blocked |
| Dependencies | relation → Tasks | |
| Milestone | relation → Milestones | |
| Output Link | url | doc, PR, brief, etc. |

## 5. Agent Activity Log
Every action an agent takes, timestamped. Audit trail + notify channel.

| Property | Type | Notes |
|---|---|---|
| Action | title | short description |
| Agent | select | which specialist |
| Timestamp | date | includes time |
| Detail | rich_text | full context |
| Task | relation → Tasks | |

## 6. Approvals
Pending high-stakes actions waiting for your yes/no. One-click approve from
Notion or your phone; n8n's wait node releases or cancels the action.

| Property | Type | Notes |
|---|---|---|
| Action | title | e.g. "Send email to client@acme.com" |
| Type | select | Send Email / Merge to main / External Invite / Share File / Publish / Delete / Spend |
| Status | select | Pending / Approved / Rejected |
| Requested By | select | which agent |
| Context | rich_text | what + why + exact content to be executed |
| Requested At | date | |

## 7. Knowledge Base
Research briefs, decisions, references the agents accumulate and reuse.

| Property | Type | Notes |
|---|---|---|
| Title | title | |
| Type | select | Brief / Decision / Reference / Snippet |
| Tags | multi_select | |
| Source | url | |
| Project | relation → Projects | |
