#!/usr/bin/env python3
"""NOMAD v2 · P1-2 — create the Phase-1 schema + views in NocoDB (idempotent).

Tables (per docs/adr/phase1-contracts.md): goals, comms, outbox, episodic.
Views on comms: a lane "channel" (lane=comms) + the gate view (status=awaiting-approval).
Enums are stored as SingleLineText for Phase-1 simplicity (filters still match on eq).

Run on the host (stdlib only):  python3 v2/setup_nocodb.py
Reads NC_ADMIN_EMAIL / NC_ADMIN_PASSWORD / NC_BASE_URL from .env (or env).
"""
import json
import os
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env():
    env = dict(os.environ)
    try:
        for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, _, v = ln.partition("=")
                env.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass
    return env


ENV = load_env()
BASE = ENV.get("NC_BASE_URL", "http://localhost:8095").rstrip("/")
EMAIL = ENV["NC_ADMIN_EMAIL"]
PW = ENV["NC_ADMIN_PASSWORD"]


def req(method, path, token=None, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("xc-auth", token)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}")


def signin():
    return req("POST", "/api/v2/auth/user/signin", body={"email": EMAIL, "password": PW})["token"]


def TXT(n):  return {"column_name": n, "title": n, "uidt": "SingleLineText"}
def LONG(n): return {"column_name": n, "title": n, "uidt": "LongText"}
def NUM(n):  return {"column_name": n, "title": n, "uidt": "Number"}
def DT(n):   return {"column_name": n, "title": n, "uidt": "DateTime"}


TABLES = {
    "goals":   [TXT("title"), TXT("status"), DT("created_at")],
    "comms":   [NUM("goal_id"), TXT("run_id"), TXT("from_agent"), TXT("lane"), TXT("type"),
                TXT("status"), TXT("priority"), LONG("payload"), TXT("assigned_agent"),
                TXT("links"), DT("created_at")],
    "outbox":  [TXT("run_id"), TXT("to"), LONG("body"), DT("delivered_at")],
    "episodic": [TXT("run_id"), TXT("agent"), TXT("what"), LONG("why"), TXT("outcome"),
                 TXT("links"), DT("created_at")],
    # Phase 2A: Researcher output sink
    "knowledge": [TXT("topic"), LONG("brief"), TXT("run_id"), DT("created_at")],
    # Phase 3B: Ads/Content output sink
    "content": [TXT("topic"), LONG("content"), TXT("run_id"), DT("created_at")],
}


def main():
    token = signin()
    print(f"✓ auth ({len(token)}-char token)")

    bases = req("GET", "/api/v2/meta/bases/", token).get("list", [])
    base = next((b for b in bases if b["title"] == "NOMAD v2"), None)
    if not base:
        base = req("POST", "/api/v2/meta/bases", token, {"title": "NOMAD v2"})
    base_id = base["id"]
    print(f"✓ base 'NOMAD v2' = {base_id}")

    existing = {t["title"]: t for t in req("GET", f"/api/v2/meta/bases/{base_id}/tables", token).get("list", [])}
    tables = {}
    for name, cols in TABLES.items():
        if name in existing:
            tables[name] = existing[name]
            print(f"  · {name} exists")
        else:
            t = req("POST", f"/api/v2/meta/bases/{base_id}/tables", token,
                    {"table_name": name, "title": name, "columns": cols})
            tables[name] = t
            print(f"  + created {name} ({len(t.get('columns', []))} cols)")

    # views on comms: lane channel + gate
    comms = req("GET", f"/api/v2/meta/tables/{tables['comms']['id']}", token)
    colid = {c["title"]: c["id"] for c in comms["columns"]}
    existing_views = {v["title"] for v in comms.get("views", [])}

    def grid_with_filter(title, col, value):
        if title in existing_views:
            print(f"  · view '{title}' exists"); return
        v = req("POST", f"/api/v2/meta/tables/{tables['comms']['id']}/grids", token, {"title": title})
        vid = v["id"]
        req("POST", f"/api/v2/meta/views/{vid}/filters", token,
            {"fk_column_id": colid[col], "comparison_op": "eq", "value": value})
        print(f"  + view '{title}' ({col}={value})")

    for lane in ("comms", "research", "support", "ads", "dev"):   # one view = one channel
        grid_with_filter(f"{lane} · channel", "lane", lane)
    grid_with_filter("⚑ approval gate", "status", "awaiting-approval")
    print("✓ schema + views ready")


if __name__ == "__main__":
    main()
