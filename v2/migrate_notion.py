"""NOMAD v2 · 2B — Notion → NocoDB migration (the `migration` skill).

  python3 v2/migrate_notion.py --dry-run   # read-only snapshot (counts + schema + mapping)
  python3 v2/migrate_notion.py --load      # create tables + two-pass load + verify

Design (per MIGRATION-ROLLBACK.md):
- Mission-control tables are created SEPARATE from the pipeline's working tables: Notion Goals →
  `mc_goals`, Knowledge → `mc_knowledge` (so the engine's `goals`/`knowledge` are untouched).
- **Idempotent**: every row carries `notion_id`; a re-run skips rows already present.
- **Two-pass**: pass 1 inserts rows, pass 2 resolves relations (stores the related row's title).
- **Verified**: row counts (Notion vs NocoDB) per table at the end.
- Notion is read-only throughout; it stays the rollback source.
"""
import os
import sys

from notion_client import Client
from nocodb import NocoDB, _env

for _k, _v in _env().items():       # make .env vars (NOTION_*, NC_*) available
    os.environ.setdefault(_k, _v)


def T(n): return {"column_name": n, "title": n, "uidt": "SingleLineText"}
def L(n): return {"column_name": n, "title": n, "uidt": "LongText"}
def N(n): return {"column_name": n, "title": n, "uidt": "Number"}


# Notion DB (env key) → migration spec.
#   table, columns, fields {notion_prop: col}, rels {notion_prop: (col, related_db_key)}
M = {
    "PROJECTS": dict(table="projects",
        columns=[T("title"), T("status"), T("owner"), T("notion_id")],
        fields={"Name": "title", "Status": "status", "Owner": "owner"}, rels={}),
    "GOALS": dict(table="mc_goals",
        columns=[T("title"), T("priority"), L("description"), T("target_date"),
                 L("success_criteria"), T("project_ref"), T("notion_id")],
        fields={"Name": "title", "Priority": "priority", "Description": "description",
                "Target Date": "target_date", "Success Criteria": "success_criteria"},
        rels={"Project": ("project_ref", "PROJECTS")}),
    "MILESTONES": dict(table="milestones",
        columns=[T("title"), T("status"), N("pct_complete"), T("due_date"),
                 T("goal_ref"), T("notion_id")],
        fields={"Title": "title", "Status": "status", "% Complete": "pct_complete",
                "Due Date": "due_date"}, rels={"Goal": ("goal_ref", "GOALS")}),
    "TASKS": dict(table="tasks",
        columns=[T("title"), T("status"), T("assigned_agent"), T("output_link"),
                 T("milestone_ref"), T("notion_id")],
        fields={"Title": "title", "Status": "status", "Assigned Agent": "assigned_agent",
                "Output Link": "output_link"}, rels={"Milestone": ("milestone_ref", "MILESTONES")}),
    "ACTIVITY": dict(table="activity",
        columns=[T("title"), T("agent"), T("ts"), L("detail"), T("task_ref"), T("notion_id")],
        fields={"Action": "title", "Agent": "agent", "Timestamp": "ts", "Detail": "detail"},
        rels={"Task": ("task_ref", "TASKS")}),
    "APPROVALS": dict(table="approvals",
        columns=[T("title"), T("type"), T("status"), T("requested_by"), T("requested_at"),
                 L("context"), T("notion_id")],
        fields={"Action": "title", "Type": "type", "Status": "status",
                "Requested By": "requested_by", "Requested At": "requested_at", "Context": "context"},
        rels={}),
    "KNOWLEDGE": dict(table="mc_knowledge",
        columns=[T("title"), T("type"), T("source"), T("tags"), T("project_ref"), T("notion_id")],
        fields={"Title": "title", "Type": "type", "Source": "source", "Tags": "tags"},
        rels={"Project": ("project_ref", "PROJECTS")}),
}
ORDER = ["PROJECTS", "GOALS", "MILESTONES", "TASKS", "ACTIVITY", "APPROVALS", "KNOWLEDGE"]


def val(prop):
    t = prop.get("type")
    if t == "title":        return "".join(x["plain_text"] for x in prop["title"])
    if t == "rich_text":    return "".join(x["plain_text"] for x in prop["rich_text"])
    if t == "select":       return (prop["select"] or {}).get("name", "")
    if t == "multi_select": return ",".join(o["name"] for o in prop["multi_select"])
    if t == "number":       return prop["number"]
    if t == "date":         return (prop["date"] or {}).get("start", "")
    if t == "url":          return prop.get("url") or ""
    if t == "relation":     return [r["id"] for r in prop["relation"]]
    return ""


def notion_rows(n, db):
    ds = n.databases.retrieve(db)["data_sources"][0]["id"]
    cur, out = None, []
    while True:
        res = (n.data_sources.query(data_source_id=ds, page_size=100, start_cursor=cur)
               if cur else n.data_sources.query(data_source_id=ds, page_size=100))
        out += res.get("results", [])
        if res.get("has_more"):
            cur = res.get("next_cursor")
        else:
            return out


def dry_run():
    n = Client(auth=os.environ["NOTION_TOKEN"])
    print("NOTION → NOCODB · DRY-RUN (read-only)\n")
    total = 0
    for k in ORDER:
        db = os.environ.get(f"NOTION_DB_{k}")
        rows = notion_rows(n, db)
        total += len(rows)
        print(f"  {k:11} → nocodb.{M[k]['table']:13} rows={len(rows)}")
    print(f"\n  TOTAL {total}. Nothing written.")


def load():
    n = Client(auth=os.environ["NOTION_TOKEN"])
    db = NocoDB()
    idmap = {k: {} for k in ORDER}      # notion_page_id → (nocodb_id, title) per DB
    pending = []                        # (table, nocodb_id, col, related_db_key, [notion_ids])
    counts = {}

    print("Creating tables…")
    for k in ORDER:
        created = db.create_table(M[k]["table"], M[k]["columns"])
        print(f"  {M[k]['table']:13} {'created' if created else 'exists'}")

    print("\nPass 1 — rows…")
    for k in ORDER:
        spec = M[k]
        rows = notion_rows(n, os.environ[f"NOTION_DB_{k}"])
        counts[k] = len(rows)
        existing = {r.get("notion_id"): r for r in db.list(spec["table"], 500) if r.get("notion_id")}
        for r in rows:
            nid = r["id"]
            props = r["properties"]
            fields = {col: val(props[p]) for p, col in spec["fields"].items() if p in props}
            title = fields.get("title", "")
            fields["notion_id"] = nid
            if nid in existing:                          # idempotent skip
                idmap[k][nid] = (existing[nid]["Id"], title); continue
            rec = db.create(spec["table"], fields)
            idmap[k][nid] = (rec["Id"], title)
            for p, (col, rdb) in spec["rels"].items():
                if p in props:
                    rel_ids = val(props[p])
                    if rel_ids:
                        pending.append((spec["table"], rec["Id"], col, rdb, rel_ids))
        print(f"  {spec['table']:13} loaded {len(rows)}")

    print("\nPass 2 — relations…")
    res = 0
    for table, rid, col, rdb, rel_ids in pending:
        titles = [idmap[rdb].get(i, (None, ""))[1] for i in rel_ids]
        titles = [t for t in titles if t]
        if titles:
            db.update(table, rid, {col: ", ".join(titles)})
            res += 1
    print(f"  resolved {res} relations")

    print("\nVerify — Notion vs NocoDB counts:")
    ok = True
    for k in ORDER:
        got = len([r for r in db.list(M[k]["table"], 500) if r.get("notion_id")])
        match = got == counts[k]
        ok &= match
        print(f"  {M[k]['table']:13} notion={counts[k]:3}  nocodb={got:3}  {'✓' if match else '✗ MISMATCH'}")
    print("\nRESULT:", "VERIFIED ✅ — counts match" if ok else "MISMATCH ❌ — investigate")
    return ok


if __name__ == "__main__":
    if "--load" in sys.argv:
        sys.exit(0 if load() else 1)
    dry_run()
