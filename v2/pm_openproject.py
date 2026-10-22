"""NOMAD v2 · OpenProject as the project board (PM-01 P3, ADR-011).

With NOMAD_PM=openproject the engine keeps its projects in OpenProject instead of the NocoDB
mc_goals, milestones and tasks tables. A project is a Feature, a milestone is an Epic, and a task
is a Task. OpenProject rolls progress up to the parents itself, so nothing here recomputes a
percentage.

Every write carries the name of the agent doing it. Each lane is an OpenProject user
(`nomad-<lane>`) with its own token, so the history of a work package reads as who did what.

This module only talks to OpenProject. It decides nothing about the gate; the engine calls it
to mirror what already happened, and a failure here must never stop a run (see Engine._pm).
"""
import copy
import json
import os
import time
import urllib.parse
from collections import deque

import requests

TOKENS = os.path.expanduser(os.environ.get(
    "NOMAD_PM_TOKENS",
    "/term-secret/openproject-tokens.json" if os.path.exists("/term-secret")
    else "~/.config/nomad/openproject-tokens.json"))
PROJECT = os.environ.get("NOMAD_PM_PROJECT", "nomad-work")
# How long a board read is reused (#1110). The relay asks /project once per goal every pass, and
# each of those read the whole board: ~36 full reads a minute held OpenProject near 90% CPU. Any
# write through this client drops the cache, and the engine drops it on every OpenProject webhook,
# so the TTL only bounds what a missed webhook can leave stale.
WPS_TTL = float(os.environ.get("NOMAD_PM_WPS_TTL", "30"))
_CANDIDATES = [c for c in (
    os.environ.get("NOMAD_PM_URL"),
    "http://localhost:8097",
    "http://host.docker.internal:8097",   # from inside the engine container
) if c]
MANAGER = "nomad-manager"

# The seed's standard and container flows (nomad-pm/seed.rb), used only to plan a path of legal
# moves. OpenProject still checks every hop, so if the seed changes a stale copy here fails loudly
# on the PATCH rather than moving anything it should not.
FLOW_STANDARD = {
    "New": ["Backlog", "Ready", "Won't do", "Duplicate"],
    "Backlog": ["New", "Ready", "Won't do", "Duplicate"],
    "Ready": ["Backlog", "In Progress", "Blocked", "Won't do"],
    "In Progress": ["Ready", "Awaiting approval", "In Review", "Blocked", "Done", "Won't do"],
    # Approved / Changes requested: the operator's answer; only the Project admin and NOMAD
    # manager roles have that move (seed.rb OPERATOR_MOVES).
    "Awaiting approval": ["In Progress", "Approved", "Changes requested", "Blocked", "Done", "Won't do"],
    "Approved": ["In Progress", "Blocked", "Won't do"],
    "Changes requested": ["In Progress", "Blocked", "Won't do"],
    "In Review": ["In Progress", "Blocked", "Done"],
    "Blocked": ["Ready", "In Progress", "Won't do"],
    "Done": ["In Progress"],
    "Won't do": ["Backlog"],
    "Duplicate": ["Backlog"],
}
FLOW_CONTAINER = {
    "New": ["Backlog", "Won't do", "Duplicate"],
    "Backlog": ["In Progress", "Won't do", "Duplicate"],
    "In Progress": ["Backlog", "Blocked", "Done", "Won't do"],
    "Blocked": ["In Progress", "Won't do"],
    "Done": ["In Progress"],
    "Won't do": ["Backlog"],
    "Duplicate": ["Backlog"],
}
CONTAINERS = {"Initiative", "Feature", "Epic"}


class PMError(RuntimeError):
    pass


def path_to(flow, current, target):
    """Shortest list of statuses that walks `flow` from current to target, excluding current."""
    if current == target:
        return []
    seen, q = {current}, deque([(current, [])])
    while q:
        s, p = q.popleft()
        for n in flow.get(s, []):
            if n in seen:
                continue
            if n == target:
                return p + [n]
            seen.add(n)
            q.append((n, p + [n]))
    raise PMError(f"no path {current} -> {target}")


def _href_id(link):
    h = (link or {}).get("href")
    return int(h.rstrip("/").split("/")[-1]) if h else None


def _title(wp, field):
    return (wp["_links"].get(field) or {}).get("title")


def parent_id(wp):
    return _href_id((wp or {}).get("_links", {}).get("parent"))


def project_id(wp):
    """The id of the board a work package sits on, which need not be NOMAD_PM_PROJECT (#791)."""
    return _href_id((wp or {}).get("_links", {}).get("project"))


# "next" (PRD cockpit-remote-approval R11, #792): a Task or Bug with no open blocker, Ready before
# Backlog, then Immediate > High > Normal > Low, then the lowest WP number.
NEXT_TYPES = ("Task", "Bug")
NEXT_STATUSES = ("Ready", "Backlog")
PRIORITY_RANK = {"Immediate": 0, "High": 1, "Normal": 2, "Low": 3}


def next_order(wp):
    """Sort key for "next". A WP with no priority ranks as Normal, OpenProject's default."""
    return (NEXT_STATUSES.index(_title(wp, "status")),
            PRIORITY_RANK.get(_title(wp, "priority"), PRIORITY_RANK["Normal"]), wp["id"])


def pick_next(wps, open_blockers, statuses=NEXT_STATUSES):
    """The WP "next" takes from `wps`, or None. `open_blockers(id)` is asked only in pick order,
    and only until one comes back empty, since on the real client each ask is a request.
    `statuses` narrows the pool: the unattended sweep passes ("Ready",), because Backlog is where
    the operator parks work (#990)."""
    cands = [w for w in wps
             if _title(w, "type") in NEXT_TYPES and _title(w, "status") in statuses]
    for w in sorted(cands, key=next_order):
        if not open_blockers(w["id"]):
            return w
    return None


class OpenProject:
    def __init__(self, project=None, base=None, tokens=None):
        self.project_ident = project or PROJECT
        self._base = base.rstrip("/") if base else None
        self._tokens_path = tokens or TOKENS
        self._tokens = None
        self._cache = {}

    # ── HTTP ──
    def base(self):
        if self._base:
            return self._base
        for b in _CANDIDATES:
            try:
                requests.get(b.rstrip("/") + "/api/v3", timeout=2)
                self._base = b.rstrip("/")
                return self._base
            except requests.RequestException:
                continue
        raise PMError(f"OpenProject unreachable at {', '.join(_CANDIDATES)}")

    def _token(self, who):
        if self._tokens is None:
            try:
                self._tokens = json.load(open(self._tokens_path))
            except (OSError, ValueError) as e:
                raise PMError(f"cannot read OpenProject tokens: {type(e).__name__}") from None
        who = who if who.startswith("nomad-") else f"nomad-{who}"
        # A lane with no user of its own writes as the manager rather than not at all.
        tok = self._tokens.get(who) or self._tokens.get(MANAGER)
        if not tok:
            raise PMError(f"no OpenProject token for {who} or {MANAGER}")
        return tok

    def call(self, method, path, body=None, as_=MANAGER, ok=(200, 201, 204)):
        if method != "GET":
            self.invalidate_wps()
        url = self.base() + (path if path.startswith("/api/v3") else "/api/v3" + path)
        try:
            r = requests.request(method, url, json=body, auth=("apikey", self._token(as_)), timeout=30)
        except requests.RequestException as e:
            raise PMError(f"OpenProject unreachable: {str(e)[:150]}") from None
        try:
            d = r.json() if r.content else {}
        except ValueError:
            d = {"message": r.text[:200]}
        if r.status_code not in ok:
            details = [e.get("message") for e in d.get("_embedded", {}).get("errors", [])]
            raise PMError(f"{method} {path} -> {r.status_code}: {d.get('message', '')}"
                          + (f" ({'; '.join(details)})" if details else ""))
        return d

    def _all(self, filters, cap=2000, select=None):
        """Every work package matching filters. select trims each element to the named links
        (e.g. "id,project,status"), which makes a count across all boards cheap."""
        out, offset = [], 1
        while len(out) < cap:
            params = {"filters": json.dumps(filters), "pageSize": 200,
                      "offset": offset, "sortBy": json.dumps([["id", "asc"]])}
            if select:
                params["select"] = "total," + ",".join(f"elements/{f}" for f in select.split(","))
            q = urllib.parse.urlencode(params)
            d = self.call("GET", f"/work_packages?{q}")
            els = d.get("_embedded", {}).get("elements", [])
            out += els
            if len(els) < 200 or len(out) >= d.get("total", 0):
                break
            offset += 1
        return out[:cap]

    # ── lookups, cached per instance ──
    def _named(self, kind):
        if kind not in self._cache:
            self._cache[kind] = {e["name"]: e for e in self.call("GET", f"/{kind}")["_embedded"]["elements"]}
        return self._cache[kind]

    def type_id(self, name):
        return self._named("types")[name]["id"]

    def status_id(self, name):
        return self._named("statuses")[name]["id"]

    def closed(self, status_name):
        return bool(self._named("statuses").get(status_name, {}).get("isClosed"))

    def project(self):
        if "project" not in self._cache:
            try:
                self._cache["project"] = self.call("GET", f"/projects/{self.project_ident}")
            except PMError as e:
                raise PMError(f"OpenProject project '{self.project_ident}' is not visible to "
                              f"{MANAGER}: {e}. Set NOMAD_PM_PROJECT or seed the project.") from None
        return self._cache["project"]

    def _fields(self, schema=None):
        """{field name: customFieldN} for Tasks in the project. Lane, Run ID and Cockpit link
        are for-all fields, so the Task schema names them for every type. `schema` is a work
        package's own schema href, for one on another board (#791)."""
        key = f"cf:{schema}" if schema else "cf"
        if key not in self._cache:
            s = self.call("GET", schema or
                          f"/work_packages/schemas/{self.project()['id']}-{self.type_id('Task')}")
            self._cache[key] = {v["name"]: k for k, v in s.items() if k.startswith("customField")}
        return self._cache[key]

    def _lane_href(self, lane):
        if "lanes" not in self._cache:
            d = self.call("POST", f"/projects/{self.project()['id']}/work_packages/form",
                          {"_links": {"type": {"href": f"/api/v3/types/{self.type_id('Task')}"}}})
            vals = d["_embedded"]["schema"][self._fields()["Lane"]]["_embedded"]["allowedValues"]
            self._cache["lanes"] = {v["value"]: v["_links"]["self"]["href"] for v in vals}
        return self._cache["lanes"].get(lane.removeprefix("nomad-")) or self._cache["lanes"]["manager"]

    def lane_of(self, wp):
        return (wp["_links"].get(self._fields()["Lane"]) or {}).get("title")

    # ── work packages ──
    def get(self, wp_id):
        return self.call("GET", f"/work_packages/{int(wp_id)}")

    def create(self, type_name, subject, parent=None, lane="manager", description="", fields=None):
        """`fields` sets plain custom fields by name, such as {"Legacy ref": "nocodb:tasks/12"}."""
        links = {"type": {"href": f"/api/v3/types/{self.type_id(type_name)}"},
                 self._fields()["Lane"]: {"href": self._lane_href(lane)}}
        if parent:
            links["parent"] = {"href": f"/api/v3/work_packages/{int(parent)}"}
        body = {"subject": subject[:255], "_links": links}
        if description:
            body["description"] = {"raw": description}
        for name, value in (fields or {}).items():
            if value:
                body[self._fields()[name]] = value
        return self.call("POST", f"/projects/{self.project()['id']}/work_packages", body)

    def blocks(self, blocker, blocked):
        self.call("POST", f"/work_packages/{int(blocker)}/relations",
                  {"type": "blocks", "_links": {"to": {"href": f"/api/v3/work_packages/{int(blocked)}"}}})

    def comment(self, wp_id, text, as_=MANAGER):
        return self.call("POST", f"/work_packages/{int(wp_id)}/activities", {"comment": {"raw": text}}, as_=as_)

    def set_run(self, wp_id, run_id, link=""):
        wp = self.get(wp_id)
        cf = self._fields((wp["_links"].get("schema") or {}).get("href"))
        body = {"lockVersion": wp["lockVersion"], cf["Run ID"]: run_id}
        if link:
            body[cf["Cockpit link"]] = link
        return self.call("PATCH", f"/work_packages/{wp['id']}", body)

    def run_of(self, wp):
        """The Run ID a work package carries, or ""."""
        cf = self._fields((wp["_links"].get("schema") or {}).get("href")).get("Run ID")
        return (wp.get(cf) or "") if cf else ""

    def move(self, wp_id, target, comment="", as_=MANAGER):
        """Walk the work package to `target` through legal moves, one PATCH per hop. The comment,
        if any, goes on once at the end so the reason sits next to where it landed."""
        wp = self.get(wp_id)
        current = _title(wp, "status")
        flow = FLOW_CONTAINER if _title(wp, "type") in CONTAINERS else FLOW_STANDARD
        for step in path_to(flow, current, target):
            wp = self.call("PATCH", f"/work_packages/{wp['id']}",
                           {"lockVersion": wp["lockVersion"],
                            "_links": {"status": {"href": f"/api/v3/statuses/{self.status_id(step)}"}}},
                           as_=as_)
        if comment:
            self.comment(wp["id"], comment, as_=as_)
        return wp

    def open_blockers(self, wp_id, by_id=None):
        """Ids of the work packages blocking wp_id that are not closed yet."""
        wp_id = int(wp_id)
        out = []
        for r in self.call("GET", f"/work_packages/{wp_id}/relations")["_embedded"]["elements"]:
            f, t = _href_id(r["_links"]["from"]), _href_id(r["_links"]["to"])
            b = f if (r["type"] == "blocks" and t == wp_id) else t if (r["type"] == "blocked" and f == wp_id) else None
            if b is None:
                continue
            other = (by_id or {}).get(b) or self.get(b)
            if not self.closed(_title(other, "status")):
                out.append(b)
        return out

    def activities(self, wp_id):
        return self.call("GET", f"/work_packages/{int(wp_id)}/activities")["_embedded"]["elements"]

    def watcher_ids(self, wp_id):
        return [e["id"] for e in self.call("GET", f"/work_packages/{int(wp_id)}/watchers")["_embedded"]["elements"]]

    def user_id_of(self, login):
        """The OpenProject user id behind `login`'s own token, or None when it has none (the
        manager fallback in _token would answer with the manager's id instead)."""
        self._token(MANAGER)                            # loads the token file
        if not self._tokens.get(login):
            return None
        ids = self._cache.setdefault("user_ids", {})
        if login not in ids:
            ids[login] = self.call("GET", "/users/me", as_=login).get("id")
        return ids[login]

    def board_of(self, wp):
        """The identifier of a work package's project (the board name nomad-pm uses)."""
        href = (wp.get("_links", {}).get("project") or {}).get("href")
        idents = self._cache.setdefault("project_idents", {})
        if href and href not in idents:
            idents[href] = self.call("GET", href).get("identifier")
        return idents.get(href) if href else None

    def user_name(self, href):
        """A user's display name. Members cannot read another user's login, but they can read
        the name, and seed.rb names every agent "<Lane> (agent)"."""
        names = self._cache.setdefault("users", {})
        if href and href not in names:
            names[href] = self.call("GET", href).get("name") or href
        return names.get(href) if href else None

    def block_relations(self):
        """[(blocker id, blocked id)] for every blocks relation the manager can see."""
        out, offset = [], 1
        while True:
            q = urllib.parse.urlencode({"filters": json.dumps([{"type": {"operator": "=", "values": ["blocks"]}}]),
                                        "pageSize": 500, "offset": offset})
            d = self.call("GET", f"/relations?{q}")
            els = d.get("_embedded", {}).get("elements", [])
            for r in els:
                f, t = _href_id(r["_links"]["from"]), _href_id(r["_links"]["to"])
                out.append((f, t) if r["type"] == "blocks" else (t, f))
            if len(els) < 500 or len(out) >= d.get("total", 0):
                return out
            offset += 1

    # ── the board as the engine sees it ──
    def project_wps(self, pid=None):
        """Every work package on board `pid`, NOMAD_PM_PROJECT's when none is given. A read is
        reused for WPS_TTL seconds; callers get their own copy, so they may change it."""
        key = str(pid or self.project()["id"])
        boards = self._cache.setdefault("wps", {})
        hit = boards.get(key)
        if hit and time.monotonic() - hit[0] < WPS_TTL:
            return copy.deepcopy(hit[1])
        wps = self._all([{"project": {"operator": "=", "values": [key]}}])
        boards[key] = (time.monotonic(), wps)
        return copy.deepcopy(wps)

    def invalidate_wps(self):
        """Drop every cached board read: a work package changed."""
        self._cache.pop("wps", None)

    @staticmethod
    def tree(wps, root_id):
        """(epics, {epic id: [tasks]}) under one Feature, in id order."""
        kids = {}
        for w in wps:
            kids.setdefault(parent_id(w), []).append(w)
        epics = [w for w in kids.get(int(root_id), []) if _title(w, "type") == "Epic"]
        return epics, {e["id"]: [t for t in kids.get(e["id"], []) if _title(t, "type") != "Epic"]
                       for e in epics}
