"""NOMAD v2 · 3C test — episodic recall closes the learn-loop.

Seeds a past episode, then proves a related goal recalls it (keyword-relevant), and that an
unrelated goal still gets recent context. Cleans up.
"""
import sys

from nocodb import NocoDB
import memory

db = NocoDB()
ok = True
seeded = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"); return cond


# seed a distinctive past episode
e = db.create("episodic", {"run_id": "seed3c", "agent": "research",
    "what": "research the best local vector databases for embeddings",
    "why": "operator asked", "outcome": "save_brief → knowledge#99", "links": "test"})
seeded.append(e["Id"])

r1 = memory.recall(db, "which vector database should we use for our embedding store?")
ok &= check("related goal recalls the seeded episode", "vector databases" in r1)
ok &= check("recall is compact (lane + what + outcome)", "[research]" in r1 and "save_brief" in r1)

r2 = memory.recall(db, "send a birthday card to the team")
ok &= check("unrelated goal still returns recent context (non-empty)", len(r2) > 0)

# cleanup
for rid in seeded:
    try: db.delete("episodic", rid)
    except Exception: pass
print("  cleaned up")

print("\nRESULT:", "ALL PASS ✅" if ok else "FAILURES ❌")
sys.exit(0 if ok else 1)
