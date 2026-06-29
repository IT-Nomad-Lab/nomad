# Standalone n8n — start here

This is a self-contained n8n you bring up **before** the rest of the stack
(CLAUDE.md NEXT STEPS #2). It's where integrations and the **approval gate**
live.

## Run it
```bat
copy .env.example .env       REM then edit N8N_ENCRYPTION_KEY to a long random string
start-n8n.bat                REM or:  ./start-n8n.ps1
```
Open http://localhost:5678 and create the owner account. Stop with `stop-n8n.bat`.

## Connect the tools (NEXT STEPS #6 prerequisites)
In **Credentials**, add and test one read + one write for each:
Gmail · Google Calendar · Google Drive/Docs · Notion · GitHub.

## The approval-gate workflow (the safety valve)

The crew never performs an irreversible action itself — it calls
`request_approval`, which POSTs to an n8n webhook and files a **Pending** row in
the Notion Approvals DB. This workflow owns actually executing the action, and
only after you tap Approve.

```
[Webhook]  POST /webhook/approval
   { approval_id, action, type, context, requested_by }
        │
        ▼
[Notion] ensure/locate the Pending Approvals row  (created by the crew)
        │
        ▼
[Notify]  ping you (Notion mention + email/mobile) with action + context
        │
        ▼
[Wait]    "On webhook call" — pause until you Approve / Reject
        │           (one-click links resume this execution)
        ├── Rejected ──► [Notion] set Status = Rejected ──► end (action NEVER runs)
        │
        └── Approved ──► [Notion] set Status = Approved
                         │
                         ▼
                 [Switch on type] → the matching action node:
                   Send Email ......... Gmail: Send
                   Merge to main ...... GitHub: Merge PR
                   External Invite .... Calendar: add external attendee
                   Share File ......... Drive: share
                   Publish ............ (publish target)
                   Delete ............. (delete target)
                   Spend .............. (e.g. trigger Firefly generation)
                         │
                         ▼
                 [Notion] log result to Activity Log
```

**Guardrail rule:** every node that sends external email, merges/pushes to
`main`, shares/publishes externally, deletes, or spends money MUST sit on the
Approved branch — downstream of the Wait node — never before it.

## Wired action: Gmail send (the first real action)

`approval-gate.workflow.json` now has a real **Gmail (Send)** node on the
*Send Email* output of the type switch. The crew requests an email via the
`request_send_email(to, subject, body)` tool, which files a Pending Approvals
row and POSTs `{to, subject, body, ...}` here. The Gmail node maps:
`sendTo`/`subject`/`message` ← `$('Approval request').item.json.body.{to,subject,body}`.

**To finish it (one-time):**
1. n8n → **Credentials → New → Gmail OAuth2**, complete Google sign-in.
2. Re-import `approval-gate.workflow.json` (it has the Gmail node), open the
   **Send Email (Gmail)** node, and select your Gmail credential. Save + Activate.

**To test end-to-end:**
1. From the crew (or curl) trigger an email request → a **Pending** row appears
   in Approvals and an execution goes to **Waiting** in n8n.
2. Approve by calling the node's **signed** resume URL with an HTTP **GET**:
   `{{$execution.resumeUrl}}&decision=approved` (`&decision=rejected` to cancel).
   ⚠ n8n signs resume URLs (`?signature=…`) and the Wait node resumes on GET — a
   bare or POSTed `/webhook-waiting/<id>` is rejected ("Invalid token" / 404).
   On approve the Gmail node fires and the email sends; on reject, nothing sends.
3. **For hands-free approval:** add a node BEFORE the Wait (Gmail/Slack/Telegram
   "Send", or a Notion update) that delivers approve/reject links built from
   `{{$execution.resumeUrl}}` — so you tap from your phone/inbox.
4. (Optional) add a Notion node after Gmail to set the Approvals row
   Status = Approved + append to the Activity Log (else the row stays "Pending"
   even after the action runs).
