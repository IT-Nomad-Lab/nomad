"""Tool-design checks for nomad-pm and nomad-detect (#1016): annotations, tight input schemas,
structured output. Offline: OpenProject and the detector are faked, so nothing is written anywhere.

    mcp-local/.venv/bin/python mcp-local/tests/test_tool_design.py

Calls go through a real MCP client session (in memory), so input validation and the client's
own check of structuredContent against outputSchema both run.
"""
import asyncio
import os
import sys

from mcp.shared.memory import create_connected_server_and_client_session

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import nomad_detect  # noqa: E402
import nomad_pm as pm  # noqa: E402

results = []


def check(name, ok, detail: object = ""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"\n     {str(detail)[:400]}" if detail and not ok else ""), flush=True)


# ── a fake OpenProject, just big enough for get, search and create_plan ──

def link(kind, i, title):
    return {"href": f"/api/v3/{kind}/{i}", "title": title}


def fake_wp(i, subject, type_="Task", status="New", parent=None, children=()):
    links = {"type": link("types", 1, type_), "status": link("statuses", 1, status),
             "project": link("projects", 7, "PM test A"), "assignee": link("users", 3, "Manager (agent)"),
             "customField9": {"href": "/api/v3/custom_options/5", "title": "dev"},
             "children": [link("work_packages", c, f"child {c}") for c in children]}
    if parent:
        links["parent"] = link("work_packages", parent, f"parent {parent}")
    return {"id": i, "subject": subject, "lockVersion": 1, "description": {"raw": "the body"},
            "dueDate": "2026-12-31", "customField3": "r-42", "_links": links}


WPS = {10: fake_wp(10, "the epic", "Epic", children=(11,)), 11: fake_wp(11, "a task", parent=10)}
calls = []


def fake_call(method, path, body=None, ok=None):
    calls.append((method, path))
    if path.endswith("/watchers"):
        return {"_embedded": {"elements": [{"name": "Dev (agent)"}]}}
    if path.endswith("/activities") and method == "GET":
        return {"_embedded": {"elements": [
            {"createdAt": "2026-10-03T09:15:00Z", "comment": {"raw": "first note"},
             "_links": {"user": {"href": "/api/v3/users/3", "title": "Manager (agent)"}}},
            {"createdAt": "2026-10-03T09:16:00Z", "comment": {"raw": ""}, "_links": {"user": {"href": "x"}}}]}}
    if path.startswith("/work_packages?") and method == "GET":
        return {"total": 5, "_embedded": {"elements": [WPS[11], WPS[10]]}}
    if path.endswith("/relations") and method == "POST":
        return {}
    raise AssertionError(f"unexpected call {method} {path}")


def fake_relations(wid):
    return [{"type": "blocks", "reverseType": "blocked", "lag": 0,
             "_links": {"from": link("work_packages", 12, "the blocker"), "to": link("work_packages", wid, "a task")}}]


made = []


def fake_create(project, type, subject, parent=None, description="", lane="", assignee=""):
    i = 100 + len(made)
    made.append((type, subject, parent, lane))
    return fake_wp(i, subject, type, parent=parent)


def install_fakes():
    pm._call = fake_call
    pm._wp = lambda i: WPS[pm._id(i)]
    pm._relations = fake_relations
    pm._custom_fields = lambda pid, tid: {"Lane": "customField9", "Run ID": "customField3"}
    pm._allowed = lambda wid, lock: ["New", "Ready"]
    pm._project = lambda ref: {"id": 7, "identifier": "pm-test-a", "name": "PM test A"}
    pm._type = lambda name: (next(t for t in pm.LEVEL if t.lower() == name.strip().lower()), 1)
    pm._create = fake_create
    pm._filters = lambda *a, **k: (seen_filters.append((a, k)) or [])
    pm._queue = lambda limit, **f: []
    pm.AGENT = "nomad-dev"


seen_filters = []


async def call(session, tool, **args):
    r = await session.call_tool(tool, args)
    text = "\n".join(c.text for c in r.content if hasattr(c, "text"))
    return r, text


async def pm_checks():
    tools = {t.name: t for t in await pm.mcp.list_tools()}
    read = {"projects", "search", "get", "tree", "my_queue", "blocked", "ready", "project_summary"}
    destructive = {"transition", "update_fields", "unrelate"}
    additive = {"create", "create_plan", "comment", "watch", "relate"}
    check("nomad-pm keeps all 16 tool names", set(tools) == read | destructive | additive, sorted(tools))
    check("every nomad-pm tool has a title", all(t.title for t in tools.values()))
    check("read tools are readOnlyHint", all(tools[n].annotations.readOnlyHint is True for n in read))
    check("write tools are not readOnlyHint",
          all(tools[n].annotations.readOnlyHint is False for n in destructive | additive))
    check("transition, update_fields and unrelate are destructiveHint",
          all(tools[n].annotations.destructiveHint is True for n in destructive))
    check("create, create_plan, comment, watch, relate say destructiveHint false (only add)",
          all(tools[n].annotations.destructiveHint is False for n in additive))
    check("unrelate, transition, update_fields, watch are idempotentHint; create, comment, relate are not",
          all(tools[n].annotations.idempotentHint is True for n in ("unrelate", "transition", "update_fields", "watch"))
          and all(tools[n].annotations.idempotentHint is False for n in ("create", "create_plan", "comment", "relate")))
    check("nomad-pm is not openWorld (OpenProject is local)",
          all(t.annotations.openWorldHint is False for t in tools.values()))

    def prop(tool, name):
        return tools[tool].inputSchema["properties"][name]

    check("create.type is the hierarchy enum", prop("create", "type")["enum"] == list(pm.LEVEL), prop("create", "type"))
    check("search.type also allows empty, Milestone and Summary task",
          prop("search", "type")["enum"] == ["", *pm.LEVEL, "Milestone", "Summary task"])
    check("lane is an enum wherever it is taken",
          all(prop(t, "lane")["enum"] == ["", *pm.LANES] for t in ("search", "create", "update_fields", "ready")))
    check("relate.type is the relation enum", set(prop("relate", "type")["enum"]) == pm.RELATIONS)
    check("unrelate.type is the relation enum plus empty", set(prop("unrelate", "type")["enum"]) == pm.RELATIONS | {""})
    check("status stays free text (workflow-dependent)",
          "enum" not in prop("search", "status") and "enum" not in prop("transition", "status"))
    check("search.limit is bounded 1..200", (prop("search", "limit")["minimum"], prop("search", "limit")["maximum"]) == (1, 200))
    check("my_queue.limit is bounded 1..5000",
          (prop("my_queue", "limit")["minimum"], prop("my_queue", "limit")["maximum"]) == (1, 5000))
    check("tree.depth is bounded 1..10", (prop("tree", "depth")["minimum"], prop("tree", "depth")["maximum"]) == (1, 10))
    check("every parameter of search, create and relate is described",
          all("description" in p for t in ("search", "create", "relate") for p in tools[t].inputSchema["properties"].values()
              if t != "create" or p.get("title") != "Subject"))
    for name in ("get", "search", "create_plan"):
        check(f"{name} declares a real outputSchema", "result" not in tools[name].outputSchema.get("properties", {}),
              tools[name].outputSchema)

    install_fakes()
    async with create_connected_server_and_client_session(pm.mcp._mcp_server) as s:
        # ── bounds and enums refuse before any HTTP ──
        for args in ({"limit": 0}, {"limit": 201}, {"type": "Story"}, {"lane": "marketing"}):
            calls.clear()
            seen_filters.clear()
            r, text = await call(s, "search", **args)
            check(f"search {args} is refused before reaching OpenProject", r.isError and not calls and not seen_filters, text)
        r, text = await call(s, "my_queue", limit=5001)
        check("my_queue limit 5001 is refused", r.isError, text)
        r, text = await call(s, "my_queue", limit=5000)
        check("my_queue limit 5000 still works", not r.isError and "nothing open" in text, text)
        r, text = await call(s, "tree", id="10", depth=0)
        check("tree depth 0 is refused", r.isError, text)
        r, text = await call(s, "relate", from_id="1", type="parent", to_id="2")
        check("relate with an unknown type is refused, listing the kinds", r.isError and "blocks" in text, text)
        r, text = await call(s, "create", project="pm-test-a", type="Story", subject="x")
        check("create with a type outside the hierarchy is refused", r.isError and "Epic" in text, text)

        # ── spellings that worked before still work ──
        seen_filters.clear()
        r, text = await call(s, "search", type="user STORY", lane="nomad-research", limit=200)
        a = seen_filters[-1][0] if seen_filters else ()
        check("search normalises type case and the nomad- lane prefix", not r.isError and a[1:2] == ("User story",)
              and a[3:4] == ("research",), (text, a))
        made.clear()
        r, text = await call(s, "create", project="pm-test-a", type="task", subject="lower", lane="Dev")
        check("create accepts a lowercase type and lane", not r.isError and made == [("Task", "lower", None, "dev")],
              (text, made))
        r, text = await call(s, "unrelate", from_id="1", to_id="2", type="")
        check("unrelate with empty type still means every relation (reaches the tool)",
              "Input validation" not in text and "validation error" not in text.lower(), text)

        # ── structured output beside the unchanged text ──
        r, text = await call(s, "get", id="#11")
        sc = r.structuredContent or {}
        check("get text is the same text Python callers get", text == pm.get("11"), text)
        check("get text keeps its lines", text.splitlines()[0] == "#11 [Task] New · a task · PM test A · lane dev · @Manager (agent)"
              and "parent: #10 parent 10" in text and "blocked #12 the blocker" in text and "next statuses: New, Ready" in text
              and "2026-10-03T09:15 Manager (agent): first note" in text, text)
        check("get structuredContent carries the same facts",
              sc.get("id") == 11 and sc.get("lane") == "dev" and sc.get("parent") == {"id": 10, "subject": "parent 10"}
              and sc.get("relations") == [{"type": "blocked", "id": 12, "subject": "the blocker", "lag": None}]
              and sc.get("next_statuses") == ["New", "Ready"] and sc.get("fields") == {"Lane": "dev", "Run ID": "r-42"}
              and sc.get("due_date") == "2026-12-31" and sc.get("watchers") == ["Dev (agent)"]
              and sc.get("comments") == [{"at": "2026-10-03T09:15:00Z", "author": "Manager (agent)", "text": "first note"}],
              sc)
        r, text = await call(s, "search", text="x", limit=2)
        sc = r.structuredContent or {}
        check("search text unchanged, with the more-line", text.splitlines()[0].startswith("#11 [Task] New")
              and text.endswith("… 3 more, narrow the filters"), text)
        check("search structuredContent lists items and total",
              [i["id"] for i in sc.get("items", [])] == [11, 10] and sc.get("total") == 5, sc)
        check("Python search() still returns a string", isinstance(pm.search(text="x"), str))
        made.clear()
        plan = [{"key": "e", "type": "Epic", "subject": "E", "children": [
            {"key": "t1", "type": "Task", "subject": "T1"}, {"key": "t2", "type": "Task", "subject": "T2", "blocked_by": ["t1"]}]}]
        r, text = await call(s, "create_plan", project="pm-test-a", plan=plan, parent="")
        sc = r.structuredContent or {}
        check("create_plan text unchanged", text.startswith("created 3 work packages, 1 blocks relations\n")
              and 'keys: {"e": 100, "t1": 101, "t2": 102}' in text, text)
        check("create_plan structuredContent has created, relations and keys",
              [w["id"] for w in sc.get("created", [])] == [100, 101, 102] and sc.get("relations") == 1
              and sc.get("keys") == {"e": 100, "t1": 101, "t2": 102}, sc)
        r, text = await call(s, "get", id="nope")
        check("a refusal is still an MCP error, not a quiet success", r.isError and "not a work package id" in text, text)


async def detect_checks():
    tools = {t.name: t for t in await nomad_detect.mcp.list_tools()}
    d, lint = tools["detect_ai"], tools["lint_style"]
    check("detect_ai and lint_style keep their names", set(tools) == {"detect_ai", "lint_style"})
    check("both detector tools are readOnlyHint", d.annotations.readOnlyHint and lint.annotations.readOnlyHint)
    check("detect_ai is openWorldHint (external=True leaves the machine); lint_style is not",
          d.annotations.openWorldHint is True and lint.annotations.openWorldHint is False)
    check("detect_ai's description no longer tells the model what to do",
          "Run this on every draft" not in d.description and "before shipping" not in d.description, d.description)
    check("detect_ai documents that external sends the text out",
          "paid" in d.inputSchema["properties"]["external"]["description"])
    sent = []

    class R:
        def json(self):
            return {"ok": True, "verdict": "pass", "guidance": "reads human",
                    "style": {"score": 90, "band": "human", "metrics": {"burstiness": 0.5, "concrete_per_100w": 3},
                              "summary": {"total": 0}, "findings": []}}

    nomad_detect._base = "http://fake"
    nomad_detect.requests.post = lambda url, json=None, timeout=None: (sent.append(json) or R())
    async with create_connected_server_and_client_session(nomad_detect.mcp._mcp_server) as s:
        r, text = await call(s, "detect_ai", text="hello")
        check("detect_ai keeps external off by default and its output", sent == [{"text": "hello", "external": False}]
              and text.startswith("VERDICT: PASS"), (sent, text))


async def main():
    await pm_checks()
    await detect_checks()
    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
