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
        └── Approved ──► [Switch on type] → the matching action node:
                           Send Email ......... Gmail: Send
                           Merge to main ...... GitHub: Merge PR
                           External Invite .... Calendar: add external attendee
                           Share File ......... Drive: share
                           Publish ............ Stop and Error: not wired
                           Delete ............. Stop and Error: not wired
                           Spend .............. Stop and Error: not wired
                         │
                         ▼
                 [Notion] set Status = Approved
                         │
                         ▼
                 [Notion] log result to Activity Log
```

**Three rules, and the last two were learned the hard way.**

1. Every node that sends external email, merges to `main`, shares or publishes
   externally, deletes, or spends money MUST sit on the Approved branch,
   downstream of the Wait node, never before it.

2. **Bookkeeping goes after the action, never in front of it.** This diagram used
   to show `Approved → set Status → Switch on type`, and so did the live
   workflow. When the Notion credential went stale in a token rotation, the
   Notion node hard-failed and every approval aborted before its action ran. No
   mail sent, no PR merged, for three and a half months, and nothing looked
   wrong because nobody approved anything in that window. The Notion nodes still
   hard-fail on purpose — a breakage should show as a red execution, not a quiet
   skip — but now they can only break the record, not the action.

3. **An unwired action type refuses, it does not pass.** Publish, Delete and
   Spend used to be empty noOp nodes wired into `Mark Approved`, so approving
   one of those marked the Notion row Approved and wrote an Activity entry for
   something that never ran. They are Stop and Error nodes now, with no output.
   Approving an unwired type shows as a red execution and the row stays Pending.
   A gate that lies is worse than one that refuses. Wire a branch when a real
   request for it arrives; until then it must say no.

## Wired action: Gmail send (the first real action)

`approval-gate-v2.workflow.json` has a real **Gmail (Send)** node on the
*Send Email* output of the type switch. The crew requests an email via the
`request_send_email(to, subject, body)` tool, which files a Pending Approvals
row and POSTs `{to, subject, body, ...}` here. The Gmail node maps:
`sendTo`/`subject`/`message` ← `$('Approval request').item.json.body.{to,subject,body}`.

**To finish it (one-time):**
1. n8n → **Credentials → New → Gmail OAuth2**, complete Google sign-in.
2. Re-import `approval-gate-v2.workflow.json`, open the **Send Email (Gmail)**
   node, and select your Gmail credential. Save + Activate.

⚠ **Export before you import.** The repo copy is a snapshot, not the source of
truth; the live workflow is. Importing overwrites by id, so an out-of-date
export silently reverts whatever was fixed in the UI since. That is not
hypothetical: a June backup sitting in this folder still had the pre-fix node
order and would have reintroduced the three-month bug on any import. It has been
deleted, and git history is the backup now.

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
