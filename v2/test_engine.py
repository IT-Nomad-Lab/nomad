"""NOMAD v2 · P1-3 engine test — proves the gate pauses and only the approved path executes.

Drives two real runs through the live NocoDB + LiteLLM:
  A) approve  → exactly one outbox row + one episodic 'sent' record
  B) reject   → zero outbox rows + one episodic 'rejected' record
Plus: the gate holds (no outbox before approval) and resume is idempotent. Cleans up after.
"""
import json
import sys

from nocodb import NocoDB
from engine import Engine

db = NocoDB()
eng = Engine(db)
created = {"goals": [], "comms": [], "outbox": [], "episodic": []}


def outbox_for(run_id):
    return [r for r in db.list("outbox", 100) if r.get("run_id") == run_id]


def episodic_for(run_id):
    return [r for r in db.list("episodic", 100) if r.get("run_id") == run_id]


def track(st):
    created["goals"].append(st["goal_id"])
    created["comms"].append(st["comms_id"])


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    return cond


ok = True

# ── A) approve path ──
print("A) approve path")
a = eng.start_run("Send a short welcome note to the new operator")
track(a)
ok &= check("gate pauses (status awaiting-approval)", a["status"] == "awaiting-approval")
ok &= check("no outbox row before approval (gate holds)", len(outbox_for(a["run_id"])) == 0)
ra = eng.resume_run(a["run_id"], "approved")
ob = outbox_for(a["run_id"]); ep = episodic_for(a["run_id"])
created["outbox"] += [r["Id"] for r in ob]; created["episodic"] += [r["Id"] for r in ep]
ok &= check("exactly one outbox row after approval", len(ob) == 1)
ok &= check("one episodic 'executed' record", len(ep) == 1 and "outbox" in (ep[0].get("outcome", "")))
ok &= check("resume is idempotent", eng.resume_run(a["run_id"], "approved").get("note", "").startswith("already"))

# ── B) reject path ──
print("B) reject path")
b = eng.start_run("Email the vendor to cancel the order")
track(b)
rb = eng.resume_run(b["run_id"], "rejected")
ob2 = outbox_for(b["run_id"]); ep2 = episodic_for(b["run_id"])
created["episodic"] += [r["Id"] for r in ep2]
ok &= check("reject → zero outbox rows", len(ob2) == 0)
ok &= check("reject → one episodic 'rejected' record",
            len(ep2) == 1 and "reject" in (ep2[0].get("outcome", "").lower()))

# ── cleanup ──
print("cleanup")
for tbl in ("episodic", "outbox", "comms", "goals"):
    for rid in created[tbl]:
        try: db.delete(tbl, rid)
        except Exception: pass
print("  test rows removed")

print("\nRESULT:", "ALL PASS ✅" if ok else "FAILURES ❌")
sys.exit(0 if ok else 1)
