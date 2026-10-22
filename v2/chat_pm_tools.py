"""The cockpit chat's read-only nomad-pm tools (#969, PRD docs/prd/cockpit-chat-tools.md 4.2).

Six tools, each one entry in `TOOLS` in the same shape as `chat_tools.TOOLS` (#968): pm_search,
pm_get, pm_tree, pm_ready, pm_project_summary and pm_my_queue. Each answers with the same text the
nomad-pm MCP tool of the same name gives (`mcp-local/nomad_pm.py`), so the chat and an agent read
a ticket the same way; `test_chat_pm_tools.py` runs both against one fake OpenProject and compares.
`chat_reply` reaches them through `server.CHAT_TOOL_ALLOWLIST` (#970).

Read-only by construction. The engine's `pm_openproject.OpenProject` holds a write-capable token
and has create, comment, move and set_run, so this module doesn't use it. Every request goes
through `_request`, which refuses any method but GET before it opens a connection, and calls
`requests.get` and nothing else. Where nomad_pm would POST a form to read something, this module
does without:
- pm_get leaves out "next statuses" (nomad_pm asks a POST form for them);
- a lane filter is applied to the results here instead of resolving the lane through a POST form.

"my" in pm_my_queue is the operator's OpenProject user, NOMAD_PM_OPERATOR_ID (the id pm_follow and
the autopilot already use), not the account whose token reads the board.

The token is NOMAD_CHAT_PM_AS's entry in the OpenProject token file (default nomad-chat), falling
back to nomad-manager's the way pm_openproject does. A dedicated read-only OpenProject user for
nomad-chat is what makes the token itself read-only; until it exists, `_request` is the guarantee.

A tool that fails returns `{"error": ...}` as data, never raises (PRD R6).
"""
import json
import os
import urllib.parse

import requests

import pm_openproject

AS = os.environ.get("NOMAD_CHAT_PM_AS", "nomad-chat")
OPERATOR_ID = int(os.environ.get("NOMAD_PM_OPERATOR_ID") or 0) or None
TREE_DEPTH_MAX = 4

# Same constants as mcp-local/nomad_pm.py, so the text matches.
LEVEL = {"Initiative": 5, "Feature": 4, "Epic": 3, "User story": 2, "Task": 2, "Bug": 2, "Sub-task": 1}
PORTFOLIO = "nomad-portfolio"
QUEUE_ORDER = ["In Progress", "Approved", "Changes requested", "In Review", "Awaiting approval",
               "Blocked", "Ready", "Backlog", "New"]

_base = None
_tokens = None
_cache: dict = {}


class PMError(RuntimeError):
    pass


class ReadOnlyError(PMError):
    pass


# ── HTTP: GET only ───────────────────────────────────────────────────────────────────────────

def _url():
    global _base
    if _base:
        return _base
    for b in pm_openproject._CANDIDATES:
        try:
            requests.get(b.rstrip("/") + "/api/v3", timeout=2)
            _base = b.rstrip("/")
            return _base
        except requests.RequestException:
            continue
    raise PMError(f"OpenProject unreachable at {', '.join(pm_openproject._CANDIDATES)}")


def _token():
    global _tokens
    if _tokens is None:
        try:
            _tokens = json.load(open(pm_openproject.TOKENS))
        except (OSError, ValueError) as e:
            raise PMError(f"cannot read OpenProject tokens: {type(e).__name__}") from None
    tok = _tokens.get(AS) or _tokens.get(pm_openproject.MANAGER)
    if not tok:
        raise PMError(f"no OpenProject token for {AS} or {pm_openproject.MANAGER}")
    return tok


def _request(method, path):
    """The one way this module reaches OpenProject. Anything but GET is refused here, before a
    connection opens, and the request itself can only be a `requests.get`."""
    if method != "GET":
        raise ReadOnlyError(f"chat pm tools are read-only: {method} {path} refused")
    url = path if path.startswith("http") else _url() + (path if path.startswith("/api/v3") else "/api/v3" + path)
    try:
        r = requests.get(url, auth=("apikey", _token()), timeout=30)
    except requests.RequestException as e:
        raise PMError(f"OpenProject unreachable: {str(e)[:150]}") from None
    try:
        d = r.json() if r.content else {}
    except ValueError:
        d = {"message": r.text[:200]}
    if r.status_code != 200:
        msg = d.get("message", r.text[:200])
        details = [e.get("message") for e in d.get("_embedded", {}).get("errors", [])]
        raise PMError(f"GET {path} -> {r.status_code}: {msg}" + (f" ({'; '.join(details)})" if details else ""))
    return d


def _get(path):
    return _request("GET", path)


def _q(filters, **params):
    params["filters"] = json.dumps(filters)
    return urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, list) else v) for k, v in params.items()})


def _all(path, filters, sort=None, cap=1000):
    out, offset = [], 1
    while len(out) < cap:
        extra = {"sortBy": sort} if sort else {}
        d = _get(f"{path}?{_q(filters, pageSize=200, offset=offset, **extra)}")
        els = d.get("_embedded", {}).get("elements", [])
        out += els
        if len(els) < 200 or len(out) >= d.get("total", 0):
            break
        offset += 1
    return out[:cap]


# ── lookups, cached per process ──────────────────────────────────────────────────────────────

def _names(kind):
    if kind not in _cache:
        _cache[kind] = {e["name"]: e for e in _get(f"/{kind}")["_embedded"]["elements"]}
    return _cache[kind]


def _type(name):
    for n, e in _names("types").items():
        if n.lower() == name.strip().lower():
            return n, e["id"]
    raise PMError(f"unknown type '{name}'. Types: {', '.join(LEVEL)}")


def _status(name):
    for n, e in _names("statuses").items():
        if n.lower() == name.strip().lower():
            return n, e["id"]
    raise PMError(f"unknown status '{name}'")


def _closed_ids():
    return {e["id"] for e in _names("statuses").values() if e["isClosed"]}


def _projects():
    if "projects" not in _cache:
        _cache["projects"] = {p["identifier"]: p for p in _all("/projects", [])}
    return _cache["projects"]


def _project(ref):
    ref = str(ref).strip()
    ps = _projects()
    if ref in ps:
        return ps[ref]
    for p in ps.values():
        if str(p["id"]) == ref or p["name"].lower() == ref.lower():
            return p
    raise PMError(f"no project '{ref}'. Projects: {', '.join(ps)}")


def _custom_fields(project_id, type_id):
    key = ("cf", project_id, type_id)
    if key not in _cache:
        s = _get(f"/work_packages/schemas/{project_id}-{type_id}")
        _cache[key] = {v["name"]: k for k, v in s.items() if k.startswith("customField")}
    return _cache[key]


def _user_name(link):
    if link.get("title"):
        return link["title"]
    key = ("user", link["href"])
    if key not in _cache:
        try:
            _cache[key] = _get(link["href"])["name"]
        except PMError:
            _cache[key] = f"user {_href_id(link)}"
    return _cache[key]


def _operator():
    if not OPERATOR_ID:
        raise PMError("NOMAD_PM_OPERATOR_ID is not set, so there is no operator to read a queue for")
    return str(OPERATOR_ID)


def _principal(ref, project_id):
    """A user id from 'me' (the operator), a numeric id, a login such as nomad-manager, or a name."""
    ref = str(ref).strip()
    if ref == "me":
        return _operator()
    if ref.isdigit():
        return ref
    want = {ref.lower()}
    if ref.startswith("nomad-"):
        want.add(f"{ref.removeprefix('nomad-')} (agent)".lower())
    for p in _all("/principals", [{"member": {"operator": "=", "values": [str(project_id)]}}]):
        if {str(p.get("login", "")).lower(), p["name"].lower()} & want:
            return str(p["id"])
    raise PMError(f"no member '{ref}' in project {project_id}")


# ── work packages ────────────────────────────────────────────────────────────────────────────

def _id(ref):
    try:
        return int(str(ref).lstrip("#"))
    except ValueError:
        raise PMError(f"'{ref}' is not a work package id") from None


def _wp(wp_id):
    return _get(f"/work_packages/{_id(wp_id)}")


def _href_id(link):
    h = (link or {}).get("href")
    return int(h.rstrip("/").split("/")[-1]) if h else None


def _title(wp, field):
    return (wp["_links"].get(field) or {}).get("title")


def _lane_key(wp):
    return next((k for k, v in wp["_links"].items() if k.startswith("customField") and isinstance(v, dict)
                 and (v.get("href") or "").startswith("/api/v3/custom_options")), None)


def _lane(wp):
    cf = _lane_key(wp)
    return wp["_links"][cf]["title"] if cf else None


def _line(wp):
    """One line: #12 [Task] In Progress · subject · project · lane dev · @assignee"""
    bits = [f"#{wp['id']} [{_title(wp, 'type')}] {_title(wp, 'status')}", wp["subject"], _title(wp, "project")]
    if _lane(wp):
        bits.append(f"lane {_lane(wp)}")
    if _title(wp, "assignee"):
        bits.append(f"@{_title(wp, 'assignee')}")
    return " · ".join(b for b in bits if b)


def _relations(wp_id):
    return _get(f"/work_packages/{wp_id}/relations")["_embedded"]["elements"]


def _blockers(wp_id):
    out = []
    for r in _relations(wp_id):
        f, t = _href_id(r["_links"]["from"]), _href_id(r["_links"]["to"])
        if r["type"] == "blocks" and t == wp_id:
            out.append(f)
        elif r["type"] == "blocked" and f == wp_id:
            out.append(t)
    return out


def _open_blockers(wp_id):
    closed = _closed_ids()
    return [b for b in (_wp(x) for x in _blockers(wp_id)) if _href_id(b["_links"]["status"]) not in closed]


def _filters(project="", type="", status="", assignee="", open_only=True, text=""):
    """nomad_pm's filters without lane, which needs a POST form there; `_in_lane` does it here."""
    f = []
    if project:
        f.append({"project": {"operator": "=", "values": [str(_project(project)["id"])]}})
    if type:
        f.append({"type": {"operator": "=", "values": [str(_type(type)[1])]}})
    if status:
        f.append({"status": {"operator": "=", "values": [str(_status(s)[1]) for s in status.split(",")]}})
    elif open_only:
        f.append({"status": {"operator": "o", "values": []}})
    if assignee == "none":
        f.append({"assignee": {"operator": "!*", "values": []}})
    elif assignee:
        f.append({"assignee": {"operator": "=",
                               "values": [_principal(assignee, _project(project or PORTFOLIO)["id"])]}})
    if text:
        f.append({"search": {"operator": "**", "values": [text]}})
    return f


def _in_lane(wps, lane):
    lane = lane.removeprefix("nomad-")
    return [w for w in wps if _lane(w) == lane]


def _render_tree(nodes, children, root_ids, depth):
    lines = []

    def walk(i, level):
        lines.append("  " * level + _line(nodes[i]))
        if level + 1 < depth:
            for c in children.get(i, []):
                walk(c, level + 1)
            return
        if children.get(i):
            lines.append("  " * (level + 1) + f"… {len(children[i])} more below")

    for r in root_ids:
        walk(r, 0)
    return "\n".join(lines)


# ── the six tools ────────────────────────────────────────────────────────────────────────────

def pm_search(text="", project="", type="", status="", lane="", assignee="", open_only=True, limit=25) -> str:
    """nomad_pm `search`. assignee 'me' is the operator."""
    limit = max(1, min(int(limit), 200))
    f = _filters(project, type, status, assignee, open_only, text)
    sort = [["updatedAt", "desc"]]
    if lane:
        els = _in_lane(_all("/work_packages", f, sort=sort), lane)
        total, els = len(els), els[:limit]
    else:
        d = _get(f"/work_packages?{_q(f, pageSize=limit, sortBy=sort)}")
        els, total = d["_embedded"]["elements"], d["total"]
    if not els:
        return "no matches"
    more = f"\n… {total - len(els)} more, narrow the filters" if total > len(els) else ""
    return "\n".join(_line(w) for w in els) + more


def pm_get(id) -> str:
    """nomad_pm `get`, without "next statuses" (OpenProject only answers that to a POST)."""
    wp = _wp(id)
    wid = wp["id"]
    out = [_line(wp)]
    pid = _href_id(wp["_links"]["project"])
    cf = _custom_fields(pid, _href_id(wp["_links"]["type"]))
    for name, key in cf.items():
        v = wp.get(key)
        if v is None:
            v = (wp["_links"].get(key) or {}).get("title") or (wp["_links"].get(key) or {}).get("href")
        if v:
            out.append(f"{name}: {v}")
    for label, key in [("start", "startDate"), ("due", "dueDate")]:
        if wp.get(key):
            out.append(f"{label}: {wp[key]}")
    if _title(wp, "parent"):
        out.append(f"parent: #{_href_id(wp['_links']['parent'])} {_title(wp, 'parent')}")
    kids = wp["_links"].get("children") or []
    if kids:
        out.append("children: " + ", ".join(f"#{_href_id(k)} {k['title']}" for k in kids))
    for r in _relations(wid):
        f, t = _href_id(r["_links"]["from"]), _href_id(r["_links"]["to"])
        kind, other, title = (r["type"], t, r["_links"]["to"]["title"]) if f == wid else \
            (r["reverseType"], f, r["_links"]["from"]["title"])
        out.append(f"{kind} #{other} {title}" + (f" (lag {r['lag']})" if r.get("lag") else ""))
    watchers = _get(f"/work_packages/{wid}/watchers")["_embedded"]["elements"]
    if watchers:
        out.append("watchers: " + ", ".join(w["name"] for w in watchers))
    desc = (wp.get("description") or {}).get("raw")
    if desc:
        out.append(f"\n{desc}")
    comments = [a for a in _get(f"/work_packages/{wid}/activities")["_embedded"]["elements"]
                if (a.get("comment") or {}).get("raw")][-5:]
    if comments:
        out.append("\ncomments:")
        out += [f"  {a['createdAt'][:16]} {_user_name(a['_links']['user'])}: {a['comment']['raw']}" for a in comments]
    return "\n".join(out)


def pm_tree(id="", project="", depth=4, open_only=False) -> str:
    """nomad_pm `tree`, depth capped at 4."""
    depth = max(1, min(int(depth), TREE_DEPTH_MAX))
    if id:
        root = _wp(id)
        nodes, children, frontier = {root["id"]: root}, {}, [root]
        for _ in range(depth - 1):
            nxt = []
            for n in frontier:
                for k in n["_links"].get("children") or []:
                    c = _wp(_href_id(k))
                    if open_only and _href_id(c["_links"]["status"]) in _closed_ids():
                        continue
                    nodes[c["id"]] = c
                    children.setdefault(n["id"], []).append(c["id"])
                    nxt.append(c)
            frontier = nxt
            if len(nodes) > 300:
                break
        for n in frontier:
            if n["_links"].get("children"):
                children.setdefault(n["id"], [_href_id(k) for k in n["_links"]["children"]])
        return _render_tree(nodes, children, [root["id"]], depth)
    if not project:
        raise PMError("give an id or a project")
    els = _all("/work_packages", _filters(project, open_only=open_only), sort=[["id", "asc"]])
    if not els:
        return "project has no work packages"
    nodes = {w["id"]: w for w in els}
    children, roots = {}, []
    for w in els:
        p = _href_id(w["_links"].get("parent"))
        (children.setdefault(p, []) if p in nodes else roots).append(w["id"])
    roots.sort(key=lambda i: -LEVEL.get(_title(nodes[i], "type"), 0))
    return _render_tree(nodes, children, roots, depth)


def pm_ready(project="", lane="") -> str:
    """nomad_pm `ready`: status Ready and no open blocker."""
    wps = _all("/work_packages", _filters(project, status="Ready"), cap=500)
    if lane:
        wps = _in_lane(wps, lane)
    out = [w for w in wps if not _open_blockers(w["id"])]
    return "\n".join(_line(w) for w in out) or "nothing ready"


def _stuck(project):
    wps = _all("/work_packages", _filters(project), cap=1000)
    closed = _closed_ids()
    status_of = {w["id"]: _href_id(w["_links"]["status"]) for w in wps}
    out = []
    for w in wps:
        bl = []
        for b in _blockers(w["id"]):
            if b not in status_of:
                status_of[b] = _href_id(_wp(b)["_links"]["status"])
            if status_of[b] not in closed:
                bl.append(b)
        out.append((w, bl))
    return out


def pm_project_summary(project) -> str:
    """nomad_pm `project_summary`."""
    proj = _project(project)
    base = [{"project": {"operator": "=", "values": [str(proj["id"])]}}]
    out = [f"{proj['name']} ({proj['identifier']})"]
    for label, group in [("by status", "status"), ("open by type", "type")]:
        f = base + ([{"status": {"operator": "o", "values": []}}] if group == "type" else [])
        d = _get(f"/work_packages?{_q(f, pageSize=1, groupBy=group)}")
        groups = [f"{g['value']} {g['count']}" for g in d.get("groups", [])]
        out.append(f"{label}: " + (", ".join(groups) or "none"))
    stuck = _stuck(proj["identifier"])
    n_blocked = sum(1 for w, bl in stuck if bl or _title(w, "status") == "Blocked")
    n_ready = sum(1 for w, bl in stuck if not bl and _title(w, "status") == "Ready")
    out.append(f"blocked {n_blocked} · ready {n_ready}")
    recent = _get(f"/work_packages?{_q(base, pageSize=5, sortBy=[['updatedAt', 'desc']])}")
    if recent["_embedded"]["elements"]:
        out.append("recent:")
        out += [f"  {w['updatedAt'][:16]} {_line(w)}" for w in recent["_embedded"]["elements"]]
    return "\n".join(out)


def pm_my_queue(limit=30) -> str:
    """nomad_pm `my_queue` for the operator: their open work in the order to take it. The operator
    has no lane, so there is no "unassigned in lane" part."""
    limit = max(1, min(int(limit), 200))
    who = _operator()
    mine = []
    for s in QUEUE_ORDER:
        if len(mine) >= limit:
            break
        f = _filters(status=s) + [{"assignee": {"operator": "=", "values": [who]}}]
        mine += _all("/work_packages", f, sort=[["id", "asc"]], cap=limit - len(mine))
    if not mine:
        return f"nothing open for user {who}"
    return "\n".join(["assigned to the operator:"] + ["  " + _line(w) for w in mine])


# ── the entries #970 picks from ──────────────────────────────────────────────────────────────

_PROJECT = {"type": "string", "description": "project identifier, e.g. nomad-work"}
_LANE = {"type": "string", "description": "comms, research, support, ads, dev or manager"}

TOOLS = {t["name"]: t for t in (
    {"name": "pm_search", "read_only": True, "handler": pm_search,
     "description": "Find nomad-pm work packages (OpenProject tickets). Filters combine with AND; "
                    "newest updates first. One line each: id, type, status, subject, project, "
                    "lane, assignee.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "text": {"type": "string", "description": "words in the subject or description"},
         "project": _PROJECT,
         "type": {"type": "string",
                  "description": "Initiative, Feature, Epic, User story, Task, Bug or Sub-task"},
         "status": {"type": "string",
                    "description": "one status, or several separated by commas (overrides open_only)"},
         "lane": _LANE,
         "assignee": {"type": "string",
                      "description": "'me' (the operator), 'none', a login like nomad-dev, or a user id"},
         "open_only": {"type": "boolean", "default": True},
         "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25}}}},
    {"name": "pm_get", "read_only": True, "handler": pm_get,
     "description": "One work package in full: fields, lane, parent, children, relations "
                    "(blocks, blocked by), watchers, description and the last five comments.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["id"],
                      "properties": {"id": {"type": "string", "description": "e.g. 969 or #969"}}}},
    {"name": "pm_tree", "read_only": True, "handler": pm_tree,
     "description": "The hierarchy under one work package (id) or a whole project (project), "
                    "up to 4 levels deep.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "id": {"type": "string"}, "project": _PROJECT,
         "depth": {"type": "integer", "minimum": 1, "maximum": TREE_DEPTH_MAX, "default": 4},
         "open_only": {"type": "boolean", "default": False}}}},
    {"name": "pm_ready", "read_only": True, "handler": pm_ready,
     "description": "Work that can start now: status Ready and no open blocker.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "project": _PROJECT, "lane": _LANE}}},
    {"name": "pm_project_summary", "read_only": True, "handler": pm_project_summary,
     "description": "A project at a glance: work by status, open work by type, how much is "
                    "blocked or ready, and the five most recent changes.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["project"],
                      "properties": {"project": _PROJECT}}},
    {"name": "pm_my_queue", "read_only": True, "handler": pm_my_queue,
     "description": "The operator's own open work, in the order to take it: In Progress, "
                    "Approved, Changes requested, In Review, Awaiting approval, Blocked, Ready, "
                    "Backlog, New.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 30}}}},
)}


def call(name: str, args: dict | None = None) -> dict:
    """Run one tool by name: `{"result": text}`, or `{"error": ...}` for an unknown name, bad
    arguments or a failing read. Nothing raises into the chat turn."""
    tool = TOOLS.get(name)
    if not tool:
        return {"error": f"no tool {name!r}"}
    args = args or {}
    if not isinstance(args, dict):
        return {"error": "arguments must be an object"}
    allowed = set(tool["input_schema"]["properties"])
    extra = sorted(set(args) - allowed)
    if extra:
        return {"error": f"{name} takes no argument {', '.join(extra)}"}
    missing = [k for k in tool["input_schema"].get("required", []) if not args.get(k)]
    if missing:
        return {"error": f"{name} needs {', '.join(missing)}"}
    try:
        return {"result": tool["handler"](**args)}
    except PMError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"{name} failed: {type(e).__name__}: {str(e)[:200]}"}
