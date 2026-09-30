"""NOMAD PM bridge (MCP): the OpenProject work tracker as a short, opinionated tool set.

nomad-pm (ADR-011) runs OpenProject CE at 127.0.0.1:8097. This server is how agents work it.
The hierarchy is Initiative > Feature > Epic > User story, Task and Bug > Sub-task. OpenProject
does not enforce those levels, so every write that sets a parent checks them here.

Identity: each call acts as the agent in NOMAD_PM_AS (default nomad-dev), with its token from
~/.config/nomad/openproject-tokens.json, so comments and changes carry that lane's name.

There is no delete tool. The agents' Member role cannot delete work packages either.

Tools:
  projects, search, get, tree                       read
  create, create_plan, update_fields, transition    write work packages
  comment, watch, relate, unrelate                  conversation and dependencies
  my_queue, blocked, ready, project_summary         what to work on, what is stuck
"""
import json
import os
import urllib.parse

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

AGENT = os.environ.get("NOMAD_PM_AS", "nomad-dev")
TOKENS = os.path.expanduser(os.environ.get("NOMAD_PM_TOKENS", "~/.config/nomad/openproject-tokens.json"))
_CANDIDATES = [c for c in (
    os.environ.get("NOMAD_PM_URL"),
    "http://localhost:8097",  # OpenProject answers 400 to a Host header it does not list
    "http://host.docker.internal:8097",
) if c]

# Level of each type; a parent must sit exactly one level above its child.
LEVEL = {"Initiative": 5, "Feature": 4, "Epic": 3, "User story": 2, "Task": 2, "Bug": 2, "Sub-task": 1}
PORTFOLIO = "nomad-portfolio"
RELATIONS = {"relates", "blocks", "blocked", "precedes", "follows", "duplicates", "duplicated",
             "includes", "partof", "requires", "required"}
# Order my_queue shows work in: finish what is started before picking up new.
QUEUE_ORDER = ["In Progress", "Approved", "Changes requested", "In Review", "Awaiting approval", "Blocked", "Ready", "Backlog", "New"]

_base = None
_cache: dict = {}


class PMError(ToolError):
    pass


# ── HTTP ──

def _url():
    global _base
    if _base:
        return _base
    for b in _CANDIDATES:
        try:
            requests.get(b.rstrip("/") + "/api/v3", timeout=2)
            _base = b.rstrip("/")
            return _base
        except Exception:
            continue
    _base = _CANDIDATES[0].rstrip("/")
    return _base


def _token():
    try:
        return json.load(open(TOKENS))[AGENT]
    except KeyError:
        raise PMError(f"no token for {AGENT} in {TOKENS}; run nomad-pm/seed.sh") from None


def _call(method, path, body=None, ok=(200, 201, 204)):
    url = path if path.startswith("http") else _url() + (path if path.startswith("/api/v3") else "/api/v3" + path)
    try:
        r = requests.request(method, url, json=body, auth=("apikey", _token()), timeout=30)
    except requests.RequestException as e:
        raise PMError(f"OpenProject unreachable at {_url()}: {str(e)[:150]}") from None
    try:
        d = r.json() if r.content else {}
    except ValueError:
        d = {"message": r.text[:200]}
    if r.status_code not in ok:
        msg = d.get("message", r.text[:200])
        details = [e.get("message") for e in d.get("_embedded", {}).get("errors", [])]
        raise PMError(f"{method} {path} -> {r.status_code}: {msg}" + (f" ({'; '.join(details)})" if details else ""))
    return d


def _q(filters, **params):
    params["filters"] = json.dumps(filters)
    return urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, list) else v) for k, v in params.items()})


def _all(path, filters, sort=None, cap=1000):
    """Every element of a collection, following pages up to cap."""
    out, offset = [], 1
    while len(out) < cap:
        extra = {"sortBy": sort} if sort else {}
        d = _call("GET", f"{path}?{_q(filters, pageSize=200, offset=offset, **extra)}")
        els = d.get("_embedded", {}).get("elements", [])
        out += els
        if len(els) < 200 or len(out) >= d.get("total", 0):
            break
        offset += 1
    return out[:cap]


# ── lookups, cached per process ──

def _names(kind):
    if kind not in _cache:
        _cache[kind] = {e["name"]: e for e in _call("GET", f"/{kind}")["_embedded"]["elements"]}
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
    raise PMError(f"no project '{ref}' visible to {AGENT}. Projects: {', '.join(ps)}")


def _custom_fields(project_id, type_id):
    """{field name: customFieldN} from the work package schema."""
    key = ("cf", project_id, type_id)
    if key not in _cache:
        s = _call("GET", f"/work_packages/schemas/{project_id}-{type_id}")
        _cache[key] = {v["name"]: k for k, v in s.items() if k.startswith("customField")}
    return _cache[key]


def _lane_href(project_id, lane):
    key = ("lanes", project_id)
    if key not in _cache:
        d = _call("POST", f"/projects/{project_id}/work_packages/form",
                  {"_links": {"type": {"href": f"/api/v3/types/{_type('Task')[1]}"}}})
        cf = _custom_fields(project_id, _type("Task")[1])["Lane"]
        vals = d["_embedded"]["schema"][cf]["_embedded"]["allowedValues"]
        _cache[key] = {v["value"]: v["_links"]["self"]["href"] for v in vals}
    lane = lane.removeprefix("nomad-")
    if lane not in _cache[key]:
        raise PMError(f"unknown lane '{lane}'. Lanes: {', '.join(_cache[key])}")
    return _cache[key][lane]


def _me():
    key = ("me", AGENT)
    if key not in _cache:
        _cache[key] = _call("GET", "/users/me")
    return _cache[key]


def _user_name(link):
    """17.8 sends an activity's user link without a title, so look the name up once per id."""
    if link.get("title"):
        return link["title"]
    key = ("user", link["href"])
    if key not in _cache:
        try:
            _cache[key] = _call("GET", link["href"])["name"]
        except PMError:
            _cache[key] = f"user {_href_id(link)}"
    return _cache[key]


def _principal(ref, project_id):
    """A user href from 'me', a numeric id, a login such as nomad-manager, or a name."""
    ref = str(ref).strip()
    if ref == "me":
        return f"/api/v3/users/{_me()['id']}"
    if ref.isdigit():
        return f"/api/v3/users/{ref}"
    want = {ref.lower()}
    if ref.startswith("nomad-"):  # agent logins map to the names seed.rb gives them
        want.add(f"{ref.removeprefix('nomad-')} (agent)".lower())
    for p in _all("/principals", [{"member": {"operator": "=", "values": [str(project_id)]}}]):
        if {str(p.get("login", "")).lower(), p["name"].lower()} & want:
            return p["_links"]["self"]["href"]
    raise PMError(f"no member '{ref}' in project {project_id}")


# ── work packages ──

def _id(ref):
    try:
        return int(str(ref).lstrip("#"))
    except ValueError:
        raise PMError(f"'{ref}' is not a work package id") from None


def _wp(wp_id):
    return _call("GET", f"/work_packages/{_id(wp_id)}")


def _href_id(link):
    h = (link or {}).get("href")
    return int(h.rstrip("/").split("/")[-1]) if h else None


def _title(wp, field):
    return (wp["_links"].get(field) or {}).get("title")


def _line(wp):
    """One line: #12 [Task] In Progress · subject · project · lane dev · @assignee"""
    cf = next((k for k, v in wp["_links"].items() if k.startswith("customField") and isinstance(v, dict)
               and (v.get("href") or "").startswith("/api/v3/custom_options")), None)
    bits = [f"#{wp['id']} [{_title(wp, 'type')}] {_title(wp, 'status')}", wp["subject"], _title(wp, "project")]
    if cf:
        bits.append(f"lane {wp['_links'][cf]['title']}")
    if _title(wp, "assignee"):
        bits.append(f"@{_title(wp, 'assignee')}")
    return " · ".join(b for b in bits if b)


def _check_parent(type_name, parent_type, project_ident):
    """Refuse any link OpenProject would accept but the NOMAD hierarchy forbids."""
    if type_name not in LEVEL:
        raise PMError(f"type '{type_name}' is outside the NOMAD hierarchy: {', '.join(LEVEL)}")
    if type_name == "Initiative" and project_ident != PORTFOLIO:
        raise PMError(f"Initiatives live in {PORTFOLIO}, not {project_ident}")
    if parent_type is None:
        if type_name == "Sub-task":
            raise PMError("a Sub-task needs a parent: a User story, Task or Bug")
        return
    want = LEVEL[type_name] + 1
    if LEVEL.get(parent_type) != want:
        allowed = [t for t, lv in LEVEL.items() if lv == want] or ["none, it is the top level"]
        raise PMError(f"a {type_name} cannot sit under a {parent_type}. Allowed parent: {' or '.join(allowed)}")


def _relations(wp_id):
    return _call("GET", f"/work_packages/{wp_id}/relations")["_embedded"]["elements"]


def _blockers(wp_id, rels=None):
    """Ids of the work packages that block wp_id, whichever side stored the relation."""
    out = []
    for r in rels if rels is not None else _relations(wp_id):
        f, t = _href_id(r["_links"]["from"]), _href_id(r["_links"]["to"])
        if r["type"] == "blocks" and t == wp_id:
            out.append(f)
        elif r["type"] == "blocked" and f == wp_id:
            out.append(t)
    return out


def _open_blockers(wp_id):
    closed = _closed_ids()
    return [b for b in (_wp(x) for x in _blockers(wp_id)) if _href_id(b["_links"]["status"]) not in closed]


def _filters(project="", type="", status="", lane="", assignee="", open_only=True, text=""):
    f = []
    if project:
        f.append({"project": {"operator": "=", "values": [str(_project(project)["id"])]}})
    if type:
        f.append({"type": {"operator": "=", "values": [str(_type(type)[1])]}})
    if status:
        f.append({"status": {"operator": "=", "values": [str(_status(s)[1]) for s in status.split(",")]}})
    elif open_only:
        f.append({"status": {"operator": "o", "values": []}})
    # Lane is one custom field for all projects, so any visible project resolves its key.
    pid = _project(project or PORTFOLIO)["id"]
    if lane:
        f.append({_lane_filter_key(pid): {"operator": "=", "values": [_lane_href(pid, lane).split("/")[-1]]}})
    if assignee == "none":
        f.append({"assignee": {"operator": "!*", "values": []}})
    elif assignee:
        who = "me" if assignee in ("me", AGENT) else _principal(assignee, pid).split("/")[-1]
        f.append({"assignee": {"operator": "=", "values": [who]}})
    if text:
        f.append({"search": {"operator": "**", "values": [text]}})
    return f


def _lane_filter_key(project_id):
    return _custom_fields(project_id, _type("Task")[1])["Lane"]


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


mcp = FastMCP("nomad-pm")


@mcp.tool()
def projects() -> str:
    """List the OpenProject projects this agent can see, with their identifiers. Use the
    identifier as `project` in the other tools. Initiatives live in nomad-portfolio."""
    _cache.pop("projects", None)
    return "\n".join(f"{p['identifier']} · {p['name']} (id {p['id']})" for p in _projects().values())


@mcp.tool()
def search(text: str = "", project: str = "", type: str = "", status: str = "", lane: str = "",
           assignee: str = "", open_only: bool = True, limit: int = 25) -> str:
    """Find work packages. All filters are optional and combine with AND.
    text: words in the subject or description. project: identifier. type: Initiative, Feature,
    Epic, User story, Task, Bug or Sub-task. status: one name, or several separated by commas
    (overrides open_only). lane: comms, research, support, ads, dev or manager.
    assignee: 'me', 'none', a login like nomad-manager, or a user id. Newest updates first."""
    f = _filters(project, type, status, lane, assignee, open_only, text)
    d = _call("GET", f"/work_packages?{_q(f, pageSize=min(limit, 200), sortBy=[['updatedAt', 'desc']])}")
    els = d["_embedded"]["elements"]
    if not els:
        return "no matches"
    more = f"\n… {d['total'] - len(els)} more, narrow the filters" if d["total"] > len(els) else ""
    return "\n".join(_line(w) for w in els) + more


@mcp.tool()
def get(id: str) -> str:
    """One work package in full: fields, description, parent, children, relations, the statuses
    it may move to next, and the last five comments. Use this before changing anything."""
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
    rels = _relations(wid)
    for r in rels:
        f, t = _href_id(r["_links"]["from"]), _href_id(r["_links"]["to"])
        kind, other, title = (r["type"], t, r["_links"]["to"]["title"]) if f == wid else \
            (r["reverseType"], f, r["_links"]["from"]["title"])
        out.append(f"{kind} #{other} {title}" + (f" (lag {r['lag']})" if r.get("lag") else ""))
    watchers = _call("GET", f"/work_packages/{wid}/watchers")["_embedded"]["elements"]
    if watchers:
        out.append("watchers: " + ", ".join(w["name"] for w in watchers))
    out.append("next statuses: " + (", ".join(_allowed(wid, wp["lockVersion"])) or "none"))
    desc = (wp.get("description") or {}).get("raw")
    if desc:
        out.append(f"\n{desc}")
    comments = [a for a in _call("GET", f"/work_packages/{wid}/activities")["_embedded"]["elements"]
                if (a.get("comment") or {}).get("raw")][-5:]
    if comments:
        out.append("\ncomments:")
        out += [f"  {a['createdAt'][:16]} {_user_name(a['_links']['user'])}: {a['comment']['raw']}" for a in comments]
    return "\n".join(out)


@mcp.tool()
def tree(id: str = "", project: str = "", depth: int = 4, open_only: bool = False) -> str:
    """The hierarchy under one work package (id), or a whole project's (project). depth limits
    how many levels are shown. Children in another project still appear under their parent."""
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
        for n in frontier:  # say what the depth cut hid
            if n["_links"].get("children"):
                children.setdefault(n["id"], [_href_id(k) for k in n["_links"]["children"]])
        return _render_tree(nodes, children, [root["id"]], depth)
    if not project:
        raise PMError("give an id or a project")
    f = _filters(project, open_only=open_only)
    els = _all("/work_packages", f, sort=[["id", "asc"]])
    if not els:
        return "project has no work packages"
    nodes = {w["id"]: w for w in els}
    children, roots = {}, []
    for w in els:
        p = _href_id(w["_links"].get("parent"))
        (children.setdefault(p, []) if p in nodes else roots).append(w["id"])
    roots.sort(key=lambda i: -LEVEL.get(_title(nodes[i], "type"), 0))
    return _render_tree(nodes, children, roots, depth)


def _create(project, type, subject, parent=None, description="", lane="", assignee=""):
    proj = _project(project)
    type_name, type_id = _type(type)
    parent_wp = _wp(parent) if parent else None
    _check_parent(type_name, _title(parent_wp, "type") if parent_wp else None, proj["identifier"])
    links = {"type": {"href": f"/api/v3/types/{type_id}"}}
    if parent_wp:
        links["parent"] = {"href": f"/api/v3/work_packages/{parent_wp['id']}"}
    lane = lane or AGENT.removeprefix("nomad-")
    links[_custom_fields(proj["id"], type_id)["Lane"]] = {"href": _lane_href(proj["id"], lane)}
    if assignee:
        links["assignee"] = {"href": _principal(assignee, proj["id"])}
    body = {"subject": subject, "_links": links}
    if description:
        body["description"] = {"raw": description}
    return _call("POST", f"/projects/{proj['id']}/work_packages", body)


@mcp.tool()
def create(project: str, type: str, subject: str, parent: str = "", description: str = "",
           lane: str = "", assignee: str = "") -> str:
    """Create one work package. The parent must sit exactly one level up: Feature under
    Initiative, Epic under Feature, User story, Task or Bug under Epic, Sub-task under a Story,
    Task or Bug. Anything else is refused. Initiatives go in nomad-portfolio. lane defaults to
    this agent's lane. assignee: 'me', a login such as nomad-manager, or a user id.
    For a whole Feature > Epic > Task tree, use create_plan."""
    wp = _create(project, type, subject, parent or None, description, lane, assignee)
    return "created " + _line(wp)


@mcp.tool()
def create_plan(project: str, plan: list[dict], parent: str = "") -> str:
    """Create a whole tree in one call, for example a Feature with its Epics and Tasks.
    plan is a list of nodes: {"key": "e1", "type": "Epic", "subject": "...", "description": "",
    "lane": "", "assignee": "", "blocked_by": ["t1", 1234], "children": [...]}.
    key is a short local name that other nodes' blocked_by can point at. blocked_by also takes
    existing work package ids. parent is an existing work package the top-level nodes go under.
    The whole tree is checked against the hierarchy before anything is created. OpenProject can
    refuse a node halfway, and there is no delete, so on failure the reply lists what already
    exists."""
    parent_wp = _wp(parent) if parent else None
    proj = _project(project)
    keys, order = set(), []

    def check(nodes, parent_type):
        for n in nodes:
            if not n.get("subject") or not n.get("type"):
                raise PMError(f"every node needs type and subject: {json.dumps(n)[:120]}")
            t = _type(n["type"])[0]
            _check_parent(t, parent_type, proj["identifier"])
            if n.get("key"):
                if n["key"] in keys:
                    raise PMError(f"duplicate key '{n['key']}'")
                keys.add(n["key"])
            order.append(n)
            check(n.get("children") or [], t)

    check(plan, _title(parent_wp, "type") if parent_wp else None)
    for n in order:
        for b in n.get("blocked_by") or []:
            if isinstance(b, str) and not b.lstrip("#").isdigit() and b not in keys:
                raise PMError(f"blocked_by '{b}' names no key in the plan")

    made, ids = [], {}

    def build(nodes, parent_id):
        for n in nodes:
            wp = _create(project, n["type"], n["subject"], parent_id, n.get("description", ""),
                         n.get("lane", ""), n.get("assignee", ""))
            made.append(wp)
            if n.get("key"):
                ids[n["key"]] = wp["id"]
            n["_id"] = wp["id"]
            build(n.get("children") or [], wp["id"])

    try:
        build(plan, parent_wp["id"] if parent_wp else None)
        for n in order:
            for b in n.get("blocked_by") or []:
                blocker = ids[b] if b in ids else _id(b)
                _call("POST", f"/work_packages/{blocker}/relations",
                      {"type": "blocks", "_links": {"to": {"href": f"/api/v3/work_packages/{n['_id']}"}}})
    except PMError as e:
        done = "\n".join("  " + _line(w) for w in made) or "  nothing"
        raise PMError(f"{e}\nalready created:\n{done}") from None
    relations = sum(len(n.get("blocked_by") or []) for n in order)
    return (f"created {len(made)} work packages, {relations} blocks relations\n"
            + "\n".join(_line(w) for w in made)
            + ("\nkeys: " + json.dumps(ids) if ids else ""))


def _allowed(wp_id, lock):
    d = _call("POST", f"/work_packages/{wp_id}/form", {"lockVersion": lock})
    st = d["_embedded"]["schema"]["status"]
    vals = st.get("_embedded", {}).get("allowedValues") or st.get("_links", {}).get("allowedValues") or []
    return [v.get("name") or v.get("title") for v in vals]


@mcp.tool()
def transition(id: str, status: str, comment: str = "") -> str:
    """Move a work package to another status. The workflow decides which moves are legal for
    its type, and an illegal one fails with the list of statuses it may move to. comment, if
    given, is posted with the move so the reason sits next to it in the history."""
    wp = _wp(id)
    name, sid = _status(status)
    current = _title(wp, "status")
    if name == current:
        return f"#{wp['id']} is already {name}"
    allowed = [a for a in _allowed(wp["id"], wp["lockVersion"]) if a != current]
    if name not in allowed:
        raise PMError(f"#{wp['id']} [{_title(wp, 'type')}] cannot move {current} -> {name}. "
                      f"Allowed from {current}: {', '.join(allowed) or 'none'}")
    body = {"lockVersion": wp["lockVersion"], "_links": {"status": {"href": f"/api/v3/statuses/{sid}"}}}
    wp = _call("PATCH", f"/work_packages/{wp['id']}", body)
    if comment:
        _call("POST", f"/work_packages/{wp['id']}/activities", {"comment": {"raw": comment}})
    return f"{current} -> {_line(wp)}"


@mcp.tool()
def update_fields(id: str, subject: str = "", description: str = "", lane: str = "",
                  assignee: str = "", parent: str = "", run_id: str = "", cockpit_link: str = "",
                  start_date: str = "", due_date: str = "") -> str:
    """Change fields on a work package; only the ones given change. Status goes through
    transition, and type cannot change. parent is checked against the hierarchy like create;
    'none' detaches it. assignee: 'me', 'none', a login or a user id. Dates are YYYY-MM-DD."""
    wp = _wp(id)
    pid = _href_id(wp["_links"]["project"])
    type_name = _title(wp, "type")
    cf = _custom_fields(pid, _href_id(wp["_links"]["type"]))
    body, links = {"lockVersion": wp["lockVersion"]}, {}
    if subject:
        body["subject"] = subject
    if description:
        body["description"] = {"raw": description}
    if start_date:
        body["startDate"] = start_date
    if due_date:
        body["dueDate"] = due_date
    if run_id:
        body[cf["Run ID"]] = run_id
    if cockpit_link:  # a link-format field is a plain string attribute, not a _links entry
        body[cf["Cockpit link"]] = cockpit_link
    if lane:
        links[cf["Lane"]] = {"href": _lane_href(pid, lane)}
    if assignee:
        links["assignee"] = {"href": None if assignee == "none" else _principal(assignee, pid)}
    if parent:
        proj_ident = next(p["identifier"] for p in _projects().values() if p["id"] == pid)
        if parent == "none":
            _check_parent(type_name, None, proj_ident)
            links["parent"] = {"href": None}
        else:
            pw = _wp(parent)
            _check_parent(type_name, _title(pw, "type"), proj_ident)
            links["parent"] = {"href": f"/api/v3/work_packages/{pw['id']}"}
    if links:
        body["_links"] = links
    if len(body) == 1:
        raise PMError("nothing to change: give at least one field")
    return "updated " + _line(_call("PATCH", f"/work_packages/{wp['id']}", body))


@mcp.tool()
def comment(id: str, text: str) -> str:
    """Post a comment on a work package, authored by this agent. Markdown works."""
    wid = _id(id)
    _call("POST", f"/work_packages/{wid}/activities", {"comment": {"raw": text}})
    return f"commented on #{wid} as {AGENT}"


@mcp.tool()
def watch(id: str, user: str = "me") -> str:
    """Add a watcher to a work package, so they are notified of changes. user: 'me' (this
    agent), a login such as nomad-manager, or a user id."""
    wp = _wp(id)
    href = _principal(user, _href_id(wp["_links"]["project"]))
    _call("POST", f"/work_packages/{wp['id']}/watchers", {"user": {"href": href}})
    return f"{user} now watches #{wp['id']}"


@mcp.tool()
def relate(from_id: str, type: str, to_id: str, lag: int = 0) -> str:
    """Link two work packages, across projects if needed. type is read from `from_id`:
    blocks (from must finish before to can start), blocked, precedes (a scheduling order;
    lag counts working days), follows, relates, duplicates, duplicated, includes, partof,
    requires or required. OpenProject refuses cycles."""
    if type not in RELATIONS:
        raise PMError(f"unknown relation '{type}'. Types: {', '.join(sorted(RELATIONS))}")
    a, b = _id(from_id), _id(to_id)
    body = {"type": type, "_links": {"to": {"href": f"/api/v3/work_packages/{b}"}}}
    if lag:
        body["lag"] = lag
    _call("POST", f"/work_packages/{a}/relations", body)
    return f"#{a} {type} #{b}" + (f" (lag {lag})" if lag else "")


@mcp.tool()
def unrelate(from_id: str, to_id: str, type: str = "") -> str:
    """Remove the relation between two work packages. With type, only that kind goes; without,
    every relation between the two goes. The work packages themselves are untouched."""
    a, b = _id(from_id), _id(to_id)
    gone = []
    for r in _relations(a):
        f, t = _href_id(r["_links"]["from"]), _href_id(r["_links"]["to"])
        if {f, t} != {a, b}:
            continue
        kind = r["type"] if f == a else r["reverseType"]
        if type and kind != type:
            continue
        _call("DELETE", f"/relations/{r['id']}")
        gone.append(kind)
    if not gone:
        raise PMError(f"no {type + ' ' if type else ''}relation between #{a} and #{b}")
    return f"removed #{a} {', '.join(gone)} #{b}"


@mcp.tool()
def my_queue(limit: int = 30) -> str:
    """This agent's open work: what is assigned to it, then unassigned work in its lane, in
    the order to take it: In Progress, Approved, Changes requested, In Review, Awaiting approval,
    Blocked, Ready, Backlog, New."""
    mine = _queue(limit, assignee="me")
    lane = AGENT.removeprefix("nomad-")
    try:
        free = _queue(limit, lane=lane, assignee="none")
    except PMError:
        free = []
    out = []
    if mine:
        out += ["assigned to me:"] + ["  " + _line(w) for w in mine]
    if free:
        out += [f"unassigned in lane {lane}:"] + ["  " + _line(w) for w in free]
    return "\n".join(out) or f"nothing open for {AGENT}"


def _queue(limit, **filters):
    """The first `limit` open work packages in queue order, oldest first within a status. One query
    per status, so a lane with hundreds of items still shows its In Progress work first."""
    out = []
    for s in QUEUE_ORDER:
        if len(out) >= limit:
            break
        out += _all("/work_packages", _filters(status=s, **filters), sort=[["id", "asc"]], cap=limit - len(out))
    return out


def _stuck(project):
    """(work package, [open blockers]) for everything open that is Blocked or has an open blocker."""
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


@mcp.tool()
def blocked(project: str = "") -> str:
    """Open work that cannot move: in status Blocked, or waiting on an open blocker. Each line
    names what blocks it. Leave project empty to look everywhere."""
    lines = []
    for w, bl in _stuck(project):
        if bl or _title(w, "status") == "Blocked":
            why = ("blocked by " + ", ".join(f"#{b}" for b in bl)) if bl else "status Blocked, no blocker linked"
            lines.append(f"{_line(w)}\n    {why}")
    return "\n".join(lines) or "nothing blocked"


@mcp.tool()
def ready(project: str = "", lane: str = "") -> str:
    """Work that can start now: status Ready and no open blocker. This is the pick list for
    run_next_task. Filter by project and lane if given."""
    f = _filters(project, status="Ready", lane=lane)
    out = [w for w in _all("/work_packages", f, cap=500) if not _open_blockers(w["id"])]
    return "\n".join(_line(w) for w in out) or "nothing ready"


@mcp.tool()
def project_summary(project: str) -> str:
    """A project at a glance: open work by status and by type, how much is blocked or ready,
    and the five most recent changes."""
    proj = _project(project)
    base = [{"project": {"operator": "=", "values": [str(proj["id"])]}}]
    out = [f"{proj['name']} ({proj['identifier']})"]
    for label, group in [("by status", "status"), ("open by type", "type")]:
        f = base + ([{"status": {"operator": "o", "values": []}}] if group == "type" else [])
        d = _call("GET", f"/work_packages?{_q(f, pageSize=1, groupBy=group)}")
        groups = [f"{g['value']} {g['count']}" for g in d.get("groups", [])]
        out.append(f"{label}: " + (", ".join(groups) or "none"))
    stuck = _stuck(proj["identifier"])
    n_blocked = sum(1 for w, bl in stuck if bl or _title(w, "status") == "Blocked")
    n_ready = sum(1 for w, bl in stuck if not bl and _title(w, "status") == "Ready")
    out.append(f"blocked {n_blocked} · ready {n_ready}")
    recent = _call("GET", f"/work_packages?{_q(base, pageSize=5, sortBy=[['updatedAt', 'desc']])}")
    if recent["_embedded"]["elements"]:
        out.append("recent:")
        out += [f"  {w['updatedAt'][:16]} {_line(w)}" for w in recent["_embedded"]["elements"]]
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
