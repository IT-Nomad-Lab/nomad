#!/usr/bin/env python3
"""Sync NOMAD's project list from `Nomad.md` markers into mission control.

Any directory under PROJECT_ROOTS that contains a `Nomad.md` file is a NOMAD
project. Its frontmatter is upserted into the Projects table the console reads.
Drop a Nomad.md into any repo → it joins the list.

Sink is chosen by NOMAD_SOURCE (matches the console):
  - "nocodb" (default for the live cutover): upsert into the NocoDB `projects` table,
    auto-creating the lane/stack/repo columns if they don't exist yet.
  - "notion": upsert into the Notion Projects DB (the rollback target / mirror).

Frontmatter consumed: name, status, lane, stack, repo, owner. `nomad: false` opts out.

Env: NOMAD_SOURCE, PROJECT_ROOTS (colon-sep, default "/host"), DOTENV (default
/host/nomad/.env). NocoDB: NC_BASE_URL, NC_API_TOKEN, NC_PROJECT_BASE (default
"NOMAD v2"). Notion: NOTION_TOKEN, NOTION_DB_PROJECTS.
"""
import json
import os
import urllib.request

from dotenv import load_dotenv

load_dotenv(os.environ.get("DOTENV", "/host/nomad/.env"))
ROOTS = os.environ.get("PROJECT_ROOTS", "/host").split(":")
SOURCE = os.environ.get("NOMAD_SOURCE", "nocodb").lower()

# NOMAD's own marker is named "NOMAD"; the seeded NocoDB/Notion row is more verbose.
# Map it onto the existing row so we update rather than create a near-duplicate.
TITLE_ALIASES = {"nomad": "Project NOMAD — Orchestrator Buildout"}


def frontmatter(path):
    meta = {}
    try:
        lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
    except Exception:
        return meta
    if not lines or lines[0].strip() != "---":
        return meta
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        if ":" in ln:
            k, _, v = ln.partition(":")
            meta[k.strip().lower()] = v.strip()
    return meta


def _as_list(v):
    """Parse a frontmatter scalar that may be a YAML-ish inline list into 'a, b, c'."""
    s = (v or "").strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    parts = [p.strip().strip("'\"") for p in s.split(",")]
    return ", ".join(p for p in parts if p)


def discover():
    """Return [{name, status, lane, stack, repo, owner}] for every Nomad.md project."""
    found = {}
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            mk = os.path.join(d, "Nomad.md")
            if not (os.path.isdir(d) and os.path.isfile(mk)):
                continue
            m = frontmatter(mk)
            if str(m.get("nomad", "true")).lower() == "false":
                continue
            disp = m.get("name") or name
            found[disp] = {
                "name": disp,
                "status": (m.get("status") or "Active").capitalize(),
                "lane": m.get("lane", ""),
                "stack": _as_list(m.get("stack", "")),
                "repo": m.get("repo", ""),
                "owner": m.get("owner", "operator"),
            }
    return list(found.values())


# ── NocoDB sink ─────────────────────────────────────────────────────────────────
NC_BASE = os.environ.get("NC_BASE_URL", "http://nocodb:8080").rstrip("/")
NC_TOKEN = os.environ.get("NC_API_TOKEN", "")
NC_PROJECT_BASE = os.environ.get("NC_PROJECT_BASE", "NOMAD v2")


def _nc(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(NC_BASE + path, data=data, method=method,
                               headers={"xc-token": NC_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=12) as resp:
        return json.loads(resp.read().decode() or "{}")


def _nc_sync(projects):
    base = next(b["id"] for b in _nc("GET", "/api/v2/meta/bases/")["list"]
                if b["title"] == NC_PROJECT_BASE)
    tid = {t["title"]: t["id"] for t in
           _nc("GET", f"/api/v2/meta/bases/{base}/tables")["list"]}["projects"]
    cols = {c["title"] for c in _nc("GET", f"/api/v2/meta/tables/{tid}")["columns"]}
    # Additively create the new metadata columns if the table predates them.
    for col in ("lane", "stack", "repo"):
        if col not in cols:
            _nc("POST", f"/api/v2/meta/tables/{tid}/columns",
                {"title": col, "column_name": col, "uidt": "SingleLineText"})
            print(f"  + created column '{col}'")
    rows = _nc("GET", f"/api/v2/tables/{tid}/records?limit=200")["list"]
    by_title = {(r.get("title") or "").strip().lower(): r for r in rows}
    for p in projects:
        key = TITLE_ALIASES.get(p["name"].lower(), p["name"]).strip().lower()
        fields = {"status": p["status"], "owner": p["owner"],
                  "lane": p["lane"], "stack": p["stack"], "repo": p["repo"]}
        existing = by_title.get(key)
        if existing:
            _nc("PATCH", f"/api/v2/tables/{tid}/records", {"Id": existing["Id"], **fields})
            print(f"  ok  {p['name']:<22} [{p['status']}] lane={p['lane'] or '—'} repo={p['repo'] or '—'}")
        else:
            _nc("POST", f"/api/v2/tables/{tid}/records", {"title": p["name"], **fields})
            print(f"  ADD {p['name']:<22} [{p['status']}] lane={p['lane'] or '—'} repo={p['repo'] or '—'}")


# ── Notion sink (rollback target) ───────────────────────────────────────────────
def _notion_sync(projects):
    from notion_client import Client
    token = os.environ["NOTION_TOKEN"]
    db = os.environ["NOTION_DB_PROJECTS"]
    n = Client(auth=token)

    def title(p):
        for v in p["properties"].values():
            if v["type"] == "title":
                return "".join(t["plain_text"] for t in v["title"])
        return ""

    existing = {title(r): r for r in
                n.databases.query(database_id=db, page_size=100).get("results", [])}
    for p in projects:
        name = TITLE_ALIASES.get(p["name"].lower(), p["name"])
        props = {"Status": {"select": {"name": p["status"]}}}
        if name in existing:
            n.pages.update(existing[name]["id"], properties=props)
            print(f"  ok  {p['name']} [{p['status']}]")
        else:
            n.pages.create(parent={"database_id": db}, properties={
                "Name": {"title": [{"text": {"content": name}}]},
                "Owner": {"rich_text": [{"text": {"content": p["owner"]}}]}, **props})
            print(f"  ADD {p['name']} [{p['status']}]")


def main():
    projects = discover()
    print(f"Discovered {len(projects)} Nomad.md project(s) → sink={SOURCE}: "
          f"{', '.join(p['name'] for p in projects) or '(none)'}")
    (_nc_sync if SOURCE == "nocodb" else _notion_sync)(projects)


if __name__ == "__main__":
    main()
