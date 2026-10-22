"""#969: the chat's read-only nomad-pm tools (v2/chat_pm_tools.py).

One fake OpenProject serves both chat_pm_tools and the nomad-pm MCP server
(mcp-local/nomad_pm.py), so the parity tests check the chat reads exactly what an agent reads.
The read-only tests make every non-GET path raise and compare the fake's data before and after.
"""
import copy
import importlib.util
import json
import os
import sys
import types
import urllib.parse

import pytest
import requests

import chat_pm_tools as cpt

HERE = os.path.dirname(os.path.abspath(__file__))
NOMAD_PM = os.path.join(HERE, "..", "mcp-local", "nomad_pm.py")

# ── the fake OpenProject ─────────────────────────────────────────────────────────────────────

TYPES = {1: "Feature", 2: "Epic", 3: "Task", 4: "Bug", 5: "Initiative", 6: "User story", 7: "Sub-task"}
STATUSES = {1: ("New", False), 2: ("Ready", False), 3: ("In Progress", False), 4: ("Blocked", False),
            5: ("Done", True), 6: ("Awaiting approval", False), 7: ("In Review", False),
            8: ("Backlog", False), 9: ("Approved", False), 10: ("Changes requested", False),
            11: ("Won't do", True)}
PROJECTS = {1: ("nomad-portfolio", "NOMAD portfolio"), 3: ("nomad-work", "NOMAD work")}
LANES = {11: "dev", 12: "manager"}
USERS = {4: "Josias", 7: "Dev (agent)"}
AGENT_ID = 7    # who nomad_pm's "me" is


def _ids(d, name):
    return next(k for k, v in d.items() if (v[0] if isinstance(v, tuple) else v) == name)


def _wp(id, type, status, subject, lane, parent=None, children=(), assignee=None, updated="2026-10-01T10:00:00Z",
        description=None, run_id=None):
    sid = _ids(STATUSES, status)
    tid = _ids(TYPES, type)
    links = {
        "self": {"href": f"/api/v3/work_packages/{id}", "title": subject},
        "type": {"href": f"/api/v3/types/{tid}", "title": type},
        "status": {"href": f"/api/v3/statuses/{sid}", "title": status},
        "project": {"href": "/api/v3/projects/3", "title": "NOMAD work"},
        "schema": {"href": f"/api/v3/work_packages/schemas/3-{tid}"},
        "customField1": {"href": f"/api/v3/custom_options/{_ids(LANES, lane)}", "title": lane},
        "assignee": ({"href": f"/api/v3/users/{assignee}", "title": USERS[assignee]} if assignee
                     else {"href": None}),
        "parent": ({"href": f"/api/v3/work_packages/{parent[0]}", "title": parent[1]} if parent
                   else {"href": None}),
    }
    if children:
        links["children"] = [{"href": f"/api/v3/work_packages/{c}", "title": t} for c, t in children]
    return {"_type": "WorkPackage", "id": id, "subject": subject, "lockVersion": 1, "updatedAt": updated,
            "description": {"raw": description} if description else {"raw": ""},
            "customField2": run_id, "_links": links}


def fresh_state():
    wps = [
        _wp(965, "Feature", "In Progress", "Cockpit chat tools", "manager",
            children=[(966, "Read-only tool access")], updated="2026-10-01T09:00:00Z"),
        _wp(966, "Epic", "In Progress", "Read-only tool access", "manager", parent=(965, "Cockpit chat tools"),
            children=[(968, "Cockpit tools"), (969, "Read-only nomad-pm access"), (970, "Wire the tools"),
                      (971, "Test the question set")], updated="2026-10-01T09:30:00Z"),
        _wp(968, "Task", "Done", "Cockpit tools", "dev", parent=(966, "Read-only tool access"),
            updated="2026-10-01T11:00:00Z"),
        _wp(969, "Task", "In Progress", "Read-only nomad-pm access", "dev", parent=(966, "Read-only tool access"),
            assignee=4, updated="2026-10-01T12:00:00Z", description="Give the chat the six read tools.",
            run_id="abc123"),
        _wp(970, "Task", "Ready", "Wire the tools", "dev", parent=(966, "Read-only tool access"),
            updated="2026-10-01T08:00:00Z"),
        _wp(971, "Task", "Ready", "Test the question set", "manager", parent=(966, "Read-only tool access"),
            assignee=4, updated="2026-10-01T07:00:00Z"),
        _wp(972, "Bug", "Ready", "Cockpit phone layout overflows", "dev", updated="2026-10-01T13:00:00Z"),
    ]
    rels = [
        {"id": 1, "type": "blocks", "reverseType": "blocked", "lag": 0,
         "_links": {"from": {"href": "/api/v3/work_packages/969", "title": "Read-only nomad-pm access"},
                    "to": {"href": "/api/v3/work_packages/970", "title": "Wire the tools"}}},
        {"id": 2, "type": "blocks", "reverseType": "blocked", "lag": 0,
         "_links": {"from": {"href": "/api/v3/work_packages/968", "title": "Cockpit tools"},
                    "to": {"href": "/api/v3/work_packages/972", "title": "Cockpit phone layout overflows"}}},
    ]
    activities = {969: [
        {"createdAt": "2026-10-01T18:48:00Z", "comment": {"raw": "autopilot: promoted"},
         "_links": {"user": {"href": "/api/v3/users/7", "title": "Dev (agent)"}}},
        {"createdAt": "2026-10-01T18:49:00Z", "comment": {"raw": ""},
         "_links": {"user": {"href": "/api/v3/users/7", "title": "Dev (agent)"}}},
        {"createdAt": "2026-10-01T18:50:00Z", "comment": {"raw": "looks right"},
         "_links": {"user": {"href": "/api/v3/users/4"}}},
    ]}
    watchers = {969: [{"id": 7, "name": "Dev (agent)"}]}
    return {"wps": {w["id"]: w for w in wps}, "rels": rels, "activities": activities, "watchers": watchers}


class Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


def _coll(els, total=None, **extra):
    return {"_embedded": {"elements": els}, "total": len(els) if total is None else total, **extra}


def _ref_id(v):
    return int(str(v).rstrip("/").split("/")[-1])


class FakeOP:
    def __init__(self):
        self.state = fresh_state()
        self.log = []

    def _match(self, w, filters):
        L = w["_links"]
        for f in filters:
            (key, spec), = f.items()
            op, vals = spec["operator"], spec["values"]
            if key == "project" and str(_ref_id(L["project"]["href"])) not in vals:
                return False
            if key == "type" and str(_ref_id(L["type"]["href"])) not in vals:
                return False
            if key == "status":
                sid = _ref_id(L["status"]["href"])
                if op == "o" and STATUSES[sid][1]:
                    return False
                if op == "=" and str(sid) not in vals:
                    return False
            if key == "assignee":
                a = L["assignee"]["href"]
                if op == "!*" and a:
                    return False
                if op == "=":
                    want = {str(AGENT_ID) if v == "me" else str(v) for v in vals}
                    if not a or str(_ref_id(a)) not in want:
                        return False
            if key == "search":
                t = vals[0].lower()
                if t not in w["subject"].lower() and t not in (w["description"]["raw"] or "").lower():
                    return False
            if key == "customField1" and str(_ref_id(L["customField1"]["href"])) not in vals:
                return False
        return True

    def get(self, url, auth=None, timeout=None, **kw):
        self.log.append(("GET", url))
        u = urllib.parse.urlparse(url)
        path = u.path.removeprefix("/api/v3")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        st = self.state
        parts = [p for p in path.split("/") if p]
        if not parts:
            return Resp(200, {})
        if parts == ["types"]:
            return Resp(200, _coll([{"id": k, "name": v} for k, v in TYPES.items()]))
        if parts == ["statuses"]:
            return Resp(200, _coll([{"id": k, "name": n, "isClosed": c} for k, (n, c) in STATUSES.items()]))
        if parts == ["projects"]:
            return Resp(200, _coll([{"id": k, "identifier": i, "name": n} for k, (i, n) in PROJECTS.items()]))
        if parts == ["principals"]:
            return Resp(200, _coll([{"id": 4, "name": "Josias", "login": "josias"},
                                    {"id": 7, "name": "Dev (agent)"}]))
        if parts[0] == "users" and len(parts) == 2:
            return Resp(200, {"id": int(parts[1]), "name": USERS[int(parts[1])]})
        if parts[:2] == ["work_packages", "schemas"]:
            return Resp(200, {"_type": "Schema", "customField1": {"name": "Lane"},
                              "customField2": {"name": "Run ID"}, "subject": {"name": "Subject"}})
        if parts[0] == "work_packages" and len(parts) >= 2:
            wid = int(parts[1])
            if wid not in st["wps"]:
                return Resp(404, {"message": "The requested resource could not be found."})
            if len(parts) == 2:
                return Resp(200, copy.deepcopy(st["wps"][wid]))
            if parts[2] == "relations":
                return Resp(200, _coll([r for r in st["rels"]
                                        if wid in (_ref_id(r["_links"]["from"]["href"]),
                                                   _ref_id(r["_links"]["to"]["href"]))]))
            if parts[2] == "watchers":
                return Resp(200, _coll(st["watchers"].get(wid, [])))
            if parts[2] == "activities":
                return Resp(200, _coll(st["activities"].get(wid, [])))
        if parts == ["work_packages"]:
            els = [w for w in sorted(st["wps"].values(), key=lambda w: w["id"])
                   if self._match(w, json.loads(q.get("filters", "[]")))]
            sort = json.loads(q.get("sortBy", "[]"))
            if sort == [["updatedAt", "desc"]]:
                els.sort(key=lambda w: w["updatedAt"], reverse=True)
            extra = {}
            if q.get("groupBy"):
                counts = {}
                for w in els:
                    t = w["_links"][q["groupBy"]]["title"]
                    counts[t] = counts.get(t, 0) + 1
                extra["groups"] = [{"value": k, "count": n} for k, n in counts.items()]
            size, off = int(q.get("pageSize", 20)), int(q.get("offset", 1))
            page = els[(off - 1) * size: off * size]
            return Resp(200, _coll(copy.deepcopy(page), total=len(els), **extra))
        return Resp(404, {"message": f"no route {path}"})

    def request(self, method, url, json=None, auth=None, timeout=None, **kw):
        """nomad_pm's door. GET goes to get(); the only POST it makes for a read is the form that
        lists the next statuses, answered here. Nothing else is allowed."""
        if method == "GET":
            return self.get(url)
        self.log.append((method, url))
        path = urllib.parse.urlparse(url).path
        if method == "POST" and path.endswith("/form"):
            return Resp(200, {"_embedded": {"schema": {"status": {"_embedded": {"allowedValues": [
                {"name": "In Review"}, {"name": "Blocked"}]}}}}})
        raise AssertionError(f"write reached the fake OpenProject: {method} {url}")


def _refuse(*a, **kw):
    raise AssertionError(f"chat_pm_tools used a non-GET requests call: {a[:2]}")


@pytest.fixture
def op(monkeypatch):
    fake = FakeOP()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "request", fake.request)
    for m in ("post", "put", "patch", "delete", "head", "options"):
        monkeypatch.setattr(requests, m, _refuse)
    monkeypatch.setattr(cpt, "_base", "http://op.test")
    monkeypatch.setattr(cpt, "_tokens", {"nomad-chat": "tok"})
    monkeypatch.setattr(cpt, "_cache", {})
    monkeypatch.setattr(cpt, "OPERATOR_ID", 4)
    return fake


# ── the MCP server, loaded beside the engine with a stand-in for the mcp package ─────────────

def _load_nomad_pm():
    if "mcp.server.fastmcp" not in sys.modules:
        try:
            import mcp.server.fastmcp  # noqa: F401
        except ImportError:
            class FastMCP:
                def __init__(self, name):
                    pass

                def tool(self):
                    return lambda f: f

            fm = types.ModuleType("mcp.server.fastmcp")
            fm.FastMCP = FastMCP
            ex = types.ModuleType("mcp.server.fastmcp.exceptions")
            ex.ToolError = type("ToolError", (Exception,), {})
            sys.modules.update({"mcp": types.ModuleType("mcp"), "mcp.server": types.ModuleType("mcp.server"),
                                "mcp.server.fastmcp": fm, "mcp.server.fastmcp.exceptions": ex})
    spec = importlib.util.spec_from_file_location("nomad_pm_for_parity", NOMAD_PM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mcp_pm(op, monkeypatch):
    if not os.path.exists(NOMAD_PM):
        pytest.skip("mcp-local/nomad_pm.py is not in this checkout")
    mod = _load_nomad_pm()
    monkeypatch.setattr(mod, "_base", "http://op.test")
    monkeypatch.setattr(mod, "_cache", {})
    monkeypatch.setattr(mod, "_token", lambda: "tok")
    return mod


# ── the allowlist ────────────────────────────────────────────────────────────────────────────

SIX = {"pm_search", "pm_get", "pm_tree", "pm_ready", "pm_project_summary", "pm_my_queue"}
WRITES = {"create", "create_plan", "transition", "comment", "update_fields", "relate", "unrelate",
          "watch", "move", "set_run", "blocks", "delete"}


def test_the_six_tools_and_nothing_else():
    assert set(cpt.TOOLS) == SIX
    for name, t in cpt.TOOLS.items():
        assert t["read_only"] is True
        assert callable(t["handler"])
        assert t["input_schema"]["type"] == "object"
        assert t["input_schema"]["additionalProperties"] is False


def test_no_write_tool_is_offered():
    offered = {n.removeprefix("pm_") for n in cpt.TOOLS}
    assert not offered & WRITES
    for w in WRITES:
        assert "error" in cpt.call(w, {}) and "error" in cpt.call(f"pm_{w}", {})


def test_the_module_never_touches_the_write_capable_client():
    src = open(cpt.__file__).read()
    assert "OpenProject(" not in src
    for verb in ("requests.post", "requests.put", "requests.patch", "requests.delete", "requests.request"):
        assert verb not in src


def test_request_refuses_every_method_but_get(op):
    for m in ("POST", "PATCH", "PUT", "DELETE", "post", "HEAD"):
        with pytest.raises(cpt.ReadOnlyError):
            cpt._request(m, "/work_packages/969/activities")
    assert op.log == []


# ── parity with the nomad-pm MCP tools ───────────────────────────────────────────────────────

@pytest.mark.parametrize("kw", [
    {}, {"text": "phone"}, {"project": "nomad-work", "type": "Task"}, {"status": "Ready"},
    {"status": "Done,In Progress"}, {"open_only": False, "limit": 3}, {"assignee": "none"},
    {"assignee": "4"}, {"text": "nothing like this"},
])
def test_search_matches_the_mcp_tool(mcp_pm, kw):
    assert cpt.pm_search(**kw) == mcp_pm.search(**kw)


def test_get_matches_the_mcp_tool_minus_next_statuses(mcp_pm):
    theirs = [ln for ln in mcp_pm.get("969").split("\n") if not ln.startswith("next statuses:")]
    assert cpt.pm_get("969").split("\n") == theirs
    assert cpt.pm_get("#970") == "\n".join(ln for ln in mcp_pm.get("970").split("\n")
                                           if not ln.startswith("next statuses:"))


@pytest.mark.parametrize("kw", [{"id": "965"}, {"id": "966", "depth": 1}, {"id": "965", "open_only": True},
                                {"project": "nomad-work"}, {"project": "nomad-work", "depth": 2}])
def test_tree_matches_the_mcp_tool(mcp_pm, kw):
    assert cpt.pm_tree(**kw) == mcp_pm.tree(**kw)


@pytest.mark.parametrize("kw", [{}, {"project": "nomad-work"}])
def test_ready_matches_the_mcp_tool(mcp_pm, kw):
    assert cpt.pm_ready(**kw) == mcp_pm.ready(**kw)


def test_project_summary_matches_the_mcp_tool(mcp_pm):
    assert cpt.pm_project_summary("nomad-work") == mcp_pm.project_summary("nomad-work")


# ── answers carry the real fields ────────────────────────────────────────────────────────────

def test_get_answers_from_the_work_packages_fields(op):
    out = cpt.call("pm_get", {"id": "969"})["result"]
    assert out.split("\n")[0] == ("#969 [Task] In Progress · Read-only nomad-pm access · NOMAD work · "
                                  "lane dev · @Josias")
    assert "Lane: dev" in out and "Run ID: abc123" in out
    assert "parent: #966 Read-only tool access" in out
    assert "blocks #970 Wire the tools" in out
    assert "watchers: Dev (agent)" in out
    assert "Give the chat the six read tools." in out
    assert "Josias: looks right" in out          # user name looked up through GET /users/4
    assert "next statuses" not in out


def test_ready_leaves_out_work_with_an_open_blocker(op):
    out = cpt.call("pm_ready", {"project": "nomad-work"})["result"]
    assert "#971" in out and "#972" in out       # 972's blocker 968 is Done
    assert "#970" not in out                     # blocked by 969, In Progress


def test_lane_is_filtered_without_the_form(op):
    assert cpt.call("pm_ready", {"lane": "manager"})["result"].split("\n") == [
        "#971 [Task] Ready · Test the question set · NOMAD work · lane manager · @Josias"]
    out = cpt.call("pm_search", {"lane": "nomad-dev"})["result"]
    assert {ln.split()[0] for ln in out.split("\n")} == {"#969", "#970", "#972"}
    assert all(m == "GET" for m, _ in op.log)


def test_my_queue_is_the_operators(op):
    out = cpt.call("pm_my_queue", {})["result"]
    assert out.split("\n") == [
        "assigned to the operator:",
        "  #969 [Task] In Progress · Read-only nomad-pm access · NOMAD work · lane dev · @Josias",
        "  #971 [Task] Ready · Test the question set · NOMAD work · lane manager · @Josias",
    ]


def test_my_queue_without_an_operator_says_so(op, monkeypatch):
    monkeypatch.setattr(cpt, "OPERATOR_ID", None)
    assert "NOMAD_PM_OPERATOR_ID" in cpt.call("pm_my_queue", {})["error"]


def test_search_me_is_the_operator(op):
    out = cpt.call("pm_search", {"assignee": "me"})["result"]
    assert {ln.split()[0] for ln in out.split("\n")} == {"#969", "#971"}


def test_tree_depth_is_capped_at_four(op):
    out = cpt.call("pm_tree", {"id": "965", "depth": 9})["result"]
    assert out == cpt.pm_tree(id="965", depth=4)


def test_search_tree_and_summary_return_results(op):
    assert "#972" in cpt.call("pm_search", {"text": "phone layout"})["result"]
    tree = cpt.call("pm_tree", {"id": "965"})["result"].split("\n")
    assert tree[0].startswith("#965 [Feature]") and tree[1].startswith("  #966 [Epic]")
    s = cpt.call("pm_project_summary", {"project": "nomad-work"})["result"]
    assert s.startswith("NOMAD work (nomad-work)")
    assert "blocked 1 · ready 2" in s            # 970 waits on 969; 972's blocker is Done


# ── read-only, end to end ────────────────────────────────────────────────────────────────────

def test_every_tool_leaves_the_board_unchanged(op):
    before = copy.deepcopy(op.state)
    calls = [("pm_search", {}), ("pm_search", {"lane": "dev", "assignee": "me"}),
             ("pm_get", {"id": "969"}), ("pm_tree", {"id": "965"}), ("pm_tree", {"project": "nomad-work"}),
             ("pm_ready", {}), ("pm_project_summary", {"project": "nomad-work"}), ("pm_my_queue", {})]
    for name, args in calls:
        out = cpt.call(name, args)
        assert "result" in out, (name, out)
    assert op.state == before
    assert op.log and all(m == "GET" for m, _ in op.log)


# ── errors come back as text ─────────────────────────────────────────────────────────────────

def test_a_missing_work_package_is_a_readable_error(op):
    out = cpt.call("pm_get", {"id": "99999"})
    assert set(out) == {"error"} and "404" in out["error"]


@pytest.mark.parametrize("name,args,needle", [
    ("pm_get", {"id": "abc"}, "not a work package id"),
    ("pm_get", {}, "needs id"),
    ("pm_get", {"id": "969", "status": "Done"}, "takes no argument status"),
    ("pm_project_summary", {"project": "nope"}, "no project 'nope'"),
    ("pm_search", {"status": "Shipped"}, "unknown status"),
    ("pm_tree", {}, "give an id or a project"),
    ("pm_nothing", {}, "no tool"),
])
def test_bad_calls_are_readable_errors(op, name, args, needle):
    out = cpt.call(name, args)
    assert needle in out["error"]


def test_args_must_be_an_object(op):
    assert "object" in cpt.call("pm_get", ["969"])["error"]


def test_openproject_down_is_a_readable_error(op, monkeypatch):
    def down(*a, **kw):
        raise requests.ConnectionError("connection refused")
    monkeypatch.setattr(requests, "get", down)
    out = cpt.call("pm_get", {"id": "969"})
    assert "unreachable" in out["error"]
