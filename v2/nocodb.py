"""Minimal NocoDB v2 client for the NOMAD v2 engine (stdlib only).

Resolves the 'NOMAD v2' base + table ids by name (cached) and does record CRUD via the
auto data API. NocoDB-on-Postgres is the source of truth, so the engine is otherwise stateless.
"""
import json
import os
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env():
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


class NocoDB:
    def __init__(self, base_name="NOMAD v2"):
        e = _env()
        self.base = e.get("NC_BASE_URL", "http://localhost:8095").rstrip("/")
        self.email, self.pw = e.get("NC_ADMIN_EMAIL", ""), e.get("NC_ADMIN_PASSWORD", "")
        # Prefer a long-lived API token (xc-token): it does NOT open a user session, so the
        # engine never invalidates the operator's NocoDB UI login. Falls back to signin (xc-auth).
        self.api_token = e.get("NC_API_TOKEN", "").strip()
        self.base_name = base_name
        self._token = None
        self._tables = {}

    def _req(self, method, path, body=None, _retry=True):
        r = urllib.request.Request(self.base + path,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   method=method)
        r.add_header("Content-Type", "application/json")
        if self.api_token:
            r.add_header("xc-token", self.api_token)
        elif self._token:
            r.add_header("xc-auth", self._token)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as ex:
            # JWT invalidated (another signin as the same user) → re-auth once. Not for api-token mode.
            if ex.code == 401 and _retry and not self.api_token and not path.endswith("/signin"):
                self._token = None
                self._auth()
                return self._req(method, path, body, _retry=False)
            raise RuntimeError(f"{method} {path} -> {ex.code}: {ex.read().decode()[:300]}")

    def _auth(self):
        if self.api_token:
            return None                       # API token → no user session needed
        if not self._token:
            self._token = self._req("POST", "/api/v2/auth/user/signin",
                                     {"email": self.email, "password": self.pw})["token"]
        return self._token

    def base_id(self):
        self._auth()
        if not getattr(self, "_base_id", None):
            self._base_id = next(b["id"] for b in self._req("GET", "/api/v2/meta/bases/")["list"]
                                 if b["title"] == self.base_name)
        return self._base_id

    def table_id(self, name):
        if not self._tables:
            self._tables = {t["title"]: t["id"]
                            for t in self._req("GET", f"/api/v2/meta/bases/{self.base_id()}/tables")["list"]}
        return self._tables[name]

    def create_table(self, name, columns):
        """Idempotent table create (xc-token meta API). columns = [{column_name,title,uidt}]."""
        bid = self.base_id()
        existing = {t["title"] for t in self._req("GET", f"/api/v2/meta/bases/{bid}/tables").get("list", [])}
        if name in existing:
            return False
        self._req("POST", f"/api/v2/meta/bases/{bid}/tables",
                  {"table_name": name, "title": name, "columns": columns})
        self._tables = {}          # bust cache so table_id() re-resolves
        return True

    # ── records ──
    def create(self, table, fields):
        self._auth()
        return self._req("POST", f"/api/v2/tables/{self.table_id(table)}/records", fields)

    def update(self, table, rid, fields):
        self._auth()
        return self._req("PATCH", f"/api/v2/tables/{self.table_id(table)}/records", {"Id": rid, **fields})

    def find(self, table, field, value):
        self._auth()
        path = f"/api/v2/tables/{self.table_id(table)}/records?where=({field},eq,{value})&limit=1"
        rows = self._req("GET", path).get("list", [])
        return rows[0] if rows else None

    def list(self, table, limit=50):
        self._auth()
        return self._req("GET", f"/api/v2/tables/{self.table_id(table)}/records?limit={limit}").get("list", [])

    def delete(self, table, rid):
        self._auth()
        return self._req("DELETE", f"/api/v2/tables/{self.table_id(table)}/records", {"Id": rid})
