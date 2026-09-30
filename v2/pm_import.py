"""PM-01 P5 · the one-time import into OpenProject.

Two sources go in:

- NocoDB mission control (`mc_goals`, `milestones`, `tasks`) into `nomad-work`, as goal → Feature,
  milestone → Epic, task → Task. NocoDB stays as the rollback, read-only after the cutover.
- A snapshot of the Civis, SHIFT and crypto bot plans, one project each, under an Initiative per
  product in `nomad-portfolio`. The repo doc stays the authority; the snapshot only makes the work
  visible on one board.

Every item carries its origin in the "Legacy ref" field (nocodb:tasks/47, civis:R1), and that is
the idempotency key: a rerun creates only what is missing and walks only items still at New to
their status. Everything writes through the API as nomad-manager.

    python3 pm_import.py                 # dry run: the plan, what exists, what would be made
    python3 pm_import.py --apply         # write
    python3 pm_import.py --apply --only repos
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pm_openproject import OpenProject, _title  # noqa: E402

REF = "Legacy ref"
NOTION = "Notion ID"
TODAY = dt.date.today().isoformat()

# v1 named its crews; v2 names lanes. Anything else passes through as a lane.
LANES = {"Engineering Crew": "dev", "Builder": "dev", "Writer": "comms", "Researcher": "research"}
TASK_STATUS = {"Skipped": "Won't do"}
MILESTONE_STATUS = {"Planned": "Backlog"}
CLOSED = {"Done", "Won't do", "Duplicate"}
V1_GOAL = 1          # v1 rows carry no goal or milestone link; they all served the first goal


def rollup(statuses):
    """A container's status from its children's: Done when all closed and one is Done, In
    Progress once anything has started, Backlog otherwise."""
    statuses = list(statuses)
    if statuses and all(s in CLOSED for s in statuses):
        return "Done" if "Done" in statuses else "Won't do"
    if any(s not in ("New", "Backlog", "Ready") for s in statuses):
        return "In Progress"
    return "Backlog"


def _item(project, ref, type_, subject, status, parent=None, lane="manager", description="", notion=None):
    return {"project": project, "ref": ref, "type": type_, "subject": subject, "status": status,
            "parent": parent, "lane": lane, "description": description, "notion": notion}


def _lines(*pairs):
    return "\n".join(f"{k}: {v}" for k, v in pairs if v not in (None, ""))


# ── NocoDB mission control → nomad-work ──
def nocodb_plan(goals, milestones, tasks, project="nomad-work"):
    note = f"Imported from NocoDB mission control on {TODAY} (PM-01 P5). NocoDB keeps the original row."
    by_ms, orphans = {}, []
    for t in tasks:
        (by_ms.setdefault(int(t["milestone_ref"]), []) if t.get("milestone_ref") else orphans).append(t)
    by_goal = {}
    for m in milestones:
        by_goal.setdefault(int(m["goal_ref"]) if m.get("goal_ref") else V1_GOAL, []).append(m)

    def task(t, parent):
        return _item(project, f"nocodb:tasks/{t['Id']}", "Task", t["title"],
                     TASK_STATUS.get(t["status"], t["status"]), parent,
                     LANES.get(t.get("assigned_agent"), t.get("assigned_agent") or "manager"),
                     _lines(("Source", note), ("NocoDB agent", t.get("assigned_agent")),
                            ("NocoDB status", t["status"]), ("Output", t.get("output_link"))),
                     t.get("notion_id"))

    items = []
    for g in sorted(goals, key=lambda g: g["Id"]):
        gref = f"nocodb:mc_goals/{g['Id']}"
        feature = _item(project, gref, "Feature", g["title"], None,
                        description=_lines(("Source", note), ("Description", g.get("description")),
                                           ("Success criteria", g.get("success_criteria")),
                                           ("Priority", g.get("priority")), ("Target date", g.get("target_date"))),
                        notion=g.get("notion_id"))
        items.append(feature)
        epics = []
        for m in sorted(by_goal.get(g["Id"], []), key=lambda m: m["Id"]):
            epic = _item(project, f"nocodb:milestones/{m['Id']}", "Epic", m["title"],
                         MILESTONE_STATUS.get(m["status"], m["status"]), gref,
                         description=_lines(("Source", note), ("NocoDB status", m["status"]),
                                            ("Percent complete", m.get("pct_complete")), ("Due", m.get("due_date"))),
                         notion=m.get("notion_id"))
            items.append(epic)
            epics.append(epic)
            items += [task(t, epic["ref"]) for t in sorted(by_ms.get(m["Id"], []), key=lambda t: t["Id"])]
        if g["Id"] == V1_GOAL and orphans:
            kids = [task(t, "nocodb:v1-tasks") for t in sorted(orphans, key=lambda t: t["Id"])]
            epic = _item(project, "nocodb:v1-tasks", "Epic", "Legacy v1 tasks", rollup(k["status"] for k in kids),
                         gref, description=_lines(("Source", note), ("Why", "v1 tasks carried no milestone link.")))
            items += [epic] + kids
            epics.append(epic)
        feature["status"] = rollup(e["status"] for e in epics)
    return items


# ── repo plans → one project per product, snapshot ──
def _snap(path):
    return (f"Snapshot of {path} on {TODAY} (PM-01 P5). The repo doc is the authority: change the plan "
            "there, then update this item by hand.")


CIVIS = "~/civis/poc/docs"
REPO_PLANS = [
    # (ref, type, subject, status, parent ref, project, lane, source)
    ("civis:initiative", "Initiative", "Civis", "In Progress", None, "nomad-portfolio", "manager", f"{CIVIS}/ROADMAP.md"),
    ("civis:R", "Feature", "Platform refactor (R stream)", "Backlog", "civis:initiative", "civis", "manager", f"{CIVIS}/refactoring-plan-v2.md"),
    ("civis:R1", "Epic", "R1 Manifest schema, loader and the ua and es manifests (2d)", "Backlog", "civis:R", "civis", "dev", None),
    ("civis:R2", "Epic", "R2 Adapter contract and runner, prozorro and opensanctions ported (3d)", "Backlog", "civis:R", "civis", "dev", None),
    ("civis:R3", "Epic", "R3 The 15 global adapters moved to ingestion/common/ (2d)", "Backlog", "civis:R", "civis", "dev", None),
    ("civis:R6", "Epic", "R6 data_tier column, migration 005 (1.5d)", "Backlog", "civis:R", "civis", "dev", None),
    ("civis:R4", "Epic", "R4 Engines moved to analytics/engines/ (3d)", "Backlog", "civis:R", "civis", "dev", None),
    ("civis:R5", "Epic", "R5 Feature registry and /api/capabilities (2.5d)", "Backlog", "civis:R", "civis", "dev", None),
    ("civis:R7", "Epic", "R7 Shared demo generator and bundle relocation (3d)", "Backlog", "civis:R", "civis", "dev", None),
    ("civis:R8", "Epic", "R8 Frontend generation, shim deletion and docs (2d)", "Backlog", "civis:R", "civis", "dev", None),
    ("civis:D", "Feature", "Deep tech (D stream)", "Backlog", "civis:initiative", "civis", "manager", f"{CIVIS}/deeptech-plan.md"),
    ("civis:D1", "Epic", "D1 EO chips, indices and change points over 50 DREAM sites (~2w)", "Backlog", "civis:D", "civis", "research", None),
    ("civis:D2", "Epic", "D2 Label set and validation harness (~1w)", "Backlog", "civis:D", "civis", "research", None),
    ("civis:D3", "Epic", "D3 ML change model, fine-tuned and calibrated (~2-3w)", "Backlog", "civis:D", "civis", "research", None),
    ("civis:D4", "Epic", "D4 Behaviour features and anomaly layer (~2w)", "Backlog", "civis:D", "civis", "research", None),
    ("civis:D5", "Epic", "D5 Co-bidding graph and backtest (~1-2w)", "Backlog", "civis:D", "civis", "research", None),
    ("civis:D6", "Epic", "D6 Integration (~1-2w)", "Backlog", "civis:D", "civis", "dev", None),
    ("civis:E", "Feature", "Cross-domain expansion (E stream)", "Backlog", "civis:initiative", "civis", "manager", f"{CIVIS}/expansion-research.md"),
    ("civis:E1", "Epic", "E1 Entity spine (2-3w MVP)", "Backlog", "civis:E", "civis", "dev", None),
    ("civis:E2", "Epic", "E2 Treasury reconciliation and price engine (3-4w)", "Backlog", "civis:E", "civis", "dev", None),
    ("civis:E3", "Epic", "E3 Grants pack (ES first) and research pack (4-6w)", "Backlog", "civis:E", "civis", "research", None),
    ("civis:E4", "Epic", "E4 Document intelligence (6-8w)", "Backlog", "civis:E", "civis", "dev", None),
    ("civis:E5", "Epic", "E5 Discretionary pack and collusion screens (3-4w)", "Backlog", "civis:E", "civis", "research", None),
    ("civis:E6", "Epic", "E6 Fusion and integrity (4-6w)", "Backlog", "civis:E", "civis", "dev", None),
    ("civis:F", "Feature", "Product modules on the roadmap", "Backlog", "civis:initiative", "civis", "manager", f"{CIVIS}/ROADMAP.md"),
    ("civis:F-25", "Epic", "F-25 Transparency-gap module", "Backlog", "civis:F", "civis", "dev", None),
    ("civis:org-type-ux", "Epic", "Org-type UX pass", "Backlog", "civis:F", "civis", "dev", None),
    ("civis:F-24", "Epic", "F-24 Transparency log", "Backlog", "civis:F", "civis", "dev", None),
    ("civis:psi-double-funding", "Epic", "PSI double-funding (needs a partner agency)", "Backlog", "civis:F", "civis", "research", None),

    ("shift:initiative", "Initiative", "SHIFT", "In Progress", None, "nomad-portfolio", "manager", "~/shift-poc/HANDOFF.md"),
    ("shift:next", "Feature", "Handoff next steps (2026-07-24)", "In Progress", "shift:initiative", "shift", "manager", "~/shift-poc/HANDOFF.md"),
    ("shift:next-1", "Task", "Commit the reorg, tag v0.5.0 and push. The reorg is committed (c6635b3); the tag is not there yet", "In Progress", "shift:next", "shift", "dev", None),
    ("shift:next-2", "Task", "Add setsid to start-services.sh so services survive any launcher", "Backlog", "shift:next", "shift", "dev", None),
    ("shift:next-3", "Task", "Decide the fate of the unreferenced params/ directory", "Backlog", "shift:next", "shift", "manager", None),
    ("shift:next-4", "Task", "Week 4 remainders: demo runbook, verify the chain/Strike mission on the GCS, recorded video backup", "Backlog", "shift:next", "shift", "dev", None),

    ("crypto:initiative", "Initiative", "Crypto trading bot", "Backlog", None, "nomad-portfolio", "manager", "~/crypto-trading-bot/docs/BACKLOG.md"),
    ("crypto:backlog", "Feature", "Improvement backlog", "Backlog", "crypto:initiative", "crypto", "manager", "~/crypto-trading-bot/docs/BACKLOG.md"),
    ("crypto:BL-001", "Task", "BL-001 Short-side mean reversion", "Backlog", "crypto:backlog", "crypto", "research", None),
    ("crypto:BL-002", "Task", "BL-002 Volatility-targeted position sizing", "Backlog", "crypto:backlog", "crypto", "research", None),
    ("crypto:BL-003", "Task", "BL-003 Multi-timeframe confirmation", "Backlog", "crypto:backlog", "crypto", "research", None),
    ("crypto:BL-004", "Task", "BL-004 ETH/USDT expansion", "Backlog", "crypto:backlog", "crypto", "research", None),
]


def repo_plan():
    items, source = [], {}
    for ref, type_, subject, status, parent, project, lane, path in REPO_PLANS:
        source[ref] = path or source.get(parent)
        items.append(_item(project, ref, type_, subject, status, parent, lane, _snap(source[ref])))
    return items


# ── write ──
def existing(ops):
    """{legacy ref: (project, id, status)} across every project the import touches."""
    out = {}
    for ident, op in ops.items():
        key = op._fields()[REF]
        for w in op.project_wps():
            if w.get(key):
                out[w[key]] = (ident, w["id"], _title(w, "status"))
    return out


def apply(ops, items, write=True, log=print):
    """Create what is missing, parents first, then walk every item still at New to its status.
    Returns {"created": n, "moved": n, "kept": n}."""
    have = existing(ops)
    counts = {"created": 0, "moved": 0, "kept": 0}
    for it in items:
        if it["ref"] in have:
            counts["kept"] += 1
            continue
        counts["created"] += 1
        if not write:
            have[it["ref"]] = (it["project"], None, "New")
            continue
        parent = have[it["parent"]][1] if it["parent"] else None
        wp = ops[it["project"]].create(it["type"], it["subject"], parent=parent, lane=it["lane"],
                                       description=it["description"],
                                       fields={REF: it["ref"], NOTION: it["notion"]})
        have[it["ref"]] = (it["project"], wp["id"], _title(wp, "status"))
        log(f"created {it['project']}#{wp['id']} {it['type']} {it['ref']}")
    # moves after every create, so no child is ever added under an already closed parent
    for it in items:
        project, wp_id, status = have[it["ref"]]
        if status != "New" or it["status"] in (None, "New"):
            continue
        counts["moved"] += 1
        if write:
            ops[project].move(wp_id, it["status"])
            log(f"moved {project}#{wp_id} {it['ref']} -> {it['status']}")
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    ap.add_argument("--only", choices=["nocodb", "repos"])
    a = ap.parse_args(argv)
    items = []
    if a.only != "repos":
        import nocodb
        db = nocodb.NocoDB()
        items += nocodb_plan(db.list("mc_goals", limit=500), db.list("milestones", limit=500),
                             db.list("tasks", limit=1000))
    if a.only != "nocodb":
        items += repo_plan()
    ops = {p: OpenProject(project=p) for p in dict.fromkeys(["nomad-portfolio"] + [i["project"] for i in items])}
    counts = apply(ops, items, write=a.apply)
    print(("applied: " if a.apply else "dry run: ") + ", ".join(f"{k} {v}" for k, v in counts.items())
          + f" of {len(items)} items")


if __name__ == "__main__":
    main()
