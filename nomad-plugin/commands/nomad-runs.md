---
description: Show recent NOMAD v2 pipeline runs (live from the engine), highlighting any at the gate
---

Run:

```bash
curl -s http://localhost:8099/runs
```

Present the runs as a compact table — **run_id · lane · status · when** — most-recent first.
Call out any run with status **`awaiting-approval`** (paused at the human gate, needs the operator).
If the engine is unreachable, say so and suggest checking `docker compose ps` / `localhost:8099/health`.
