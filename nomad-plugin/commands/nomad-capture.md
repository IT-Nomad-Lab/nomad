---
description: Capture a goal into the NOMAD v2 pipeline (it clarifies, routes to a lane, and pauses at the human gate)
argument-hint: <goal text>
---

Capture the operator's goal into NOMAD v2 by calling the engine, then report what happened.

Run this, with the goal text `$ARGUMENTS` written into the JSON body yourself, escaped as a JSON
string (quotes, backslashes, newlines). The quoted heredoc keeps the shell out of it, and it needs
no Python, so it works in WSL and in Git Bash on Windows:

```bash
curl -s http://localhost:8099/capture -H 'Content-Type: application/json' --data-binary @- <<'JSON'
{"goal": "<the goal, JSON-escaped>"}
JSON
```

Then summarize the JSON for the operator: the **run_id**, the **lane** it routed to, the proposed
**action**, and that its **status is `awaiting-approval`** (paused at the human gate). Remind them
to approve or reject with `/nomad-approve <run_id> approve|reject`, or by flipping the row status
in NocoDB (localhost:8095). Nothing executes until approved.
