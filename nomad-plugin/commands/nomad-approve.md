---
description: Approve or reject a paused NOMAD v2 run at the human gate (this triggers/blocks the real action)
argument-hint: <run_id> approve|reject
---

The operator is making the **human-gate decision** for a paused run. Parse `$ARGUMENTS` as
`<run_id> <approve|reject>`. **Approving runs a real, possibly irreversible action** (e.g. sending
an email, dispatching a build) — confirm the run_id and decision with the operator before sending
if there is any ambiguity.

Run (map approve→approved, reject→rejected):

```bash
curl -s http://localhost:8099/resume -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;a=sys.argv[1:];print(json.dumps({"run_id":a[0],"decision":"approved" if a[1].startswith("a") else "rejected"}))' $ARGUMENTS)"
```

Report the result: on approve, the action that executed (and any outbox/knowledge/build output);
on reject, that it was declined and **no action ran**. The engine's app-layer interlock will refuse
to act on a run that wasn't approved.
