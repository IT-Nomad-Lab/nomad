"""NOMAD v2 · 2A test — multi-specialist routing + the research lane gate.

Proves: the Manager routes a comms goal → comms (send_message) and a research goal → research
(save_brief); the research lane gates correctly (approve → one knowledge row; reject → none).
Cleans up after.
"""
import sys

from nocodb import NocoDB
from engine import Engine

db = NocoDB()
eng = Engine(db)
created = {"goals": [], "comms": [], "knowledge": [], "outbox": [], "episodic": []}
ok = True


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"); return cond


def track(st):
    created["goals"].append(st["goal_id"]); created["comms"].append(st["comms_id"])


def knowledge_for(rid):
    return [r for r in db.list("knowledge", 100) if r.get("run_id") == rid]


# ── routing ──
print("routing")
c = eng.start_run("Send a short thank-you note to the operator"); track(c)
ok &= check("message goal → comms lane", c["lane"] == "comms")
ok &= check("comms proposes send_message", c["proposal"]["action"] == "send_message")

r = eng.start_run("Research the best local vector databases for a 24GB GPU and recommend one"); track(r)
ok &= check("question goal → research lane", r["lane"] == "research")
ok &= check("research proposes save_brief", r["proposal"]["action"] == "save_brief")

# ── research lane: approve → one knowledge row ──
print("research gate (approve)")
ok &= check("no knowledge row before approval", len(knowledge_for(r["run_id"])) == 0)
res = eng.resume_run(r["run_id"], "approved")
kn = knowledge_for(r["run_id"]); created["knowledge"] += [x["Id"] for x in kn]
ok &= check("approve → exactly one knowledge row", len(kn) == 1)
ok &= check("run executed via save_brief", res.get("action") == "save_brief" and res.get("status") == "executed")

# ── research lane: reject → no knowledge row ──
print("research gate (reject)")
r2 = eng.start_run("Investigate which TTS engines run locally on the GPU"); track(r2)
eng.resume_run(r2["run_id"], "rejected")
ok &= check("reject → zero knowledge rows", len(knowledge_for(r2["run_id"])) == 0)

# clean the comms run's outbox (approve it so cleanup is complete), then teardown
eng.resume_run(c["run_id"], "rejected")
print("cleanup")
for tbl in ("episodic", "knowledge", "outbox", "comms", "goals"):
    for rid in created[tbl] + [x["Id"] for x in (db.list("episodic", 100) if tbl == "episodic" else [])
                               if x.get("run_id") in [c["run_id"], r["run_id"], r2["run_id"]]]:
        try: db.delete(tbl, rid)
        except Exception: pass
print("  test rows removed")

print("\nRESULT:", "ALL PASS ✅" if ok else "FAILURES ❌")
sys.exit(0 if ok else 1)
