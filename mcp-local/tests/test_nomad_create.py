"""nomad-create (#1374): create_project forwards the answers to the dispatcher's POST /create-project
and reports the run; nothing is made here. Offline: the dispatcher is faked.

    mcp-local/.venv/bin/python mcp-local/tests/test_nomad_create.py

Calls go through a real MCP client session (in memory), so input validation and the client's
own check of structuredContent against outputSchema both run.
"""
import asyncio
import os
import sys

from mcp.shared.memory import create_connected_server_and_client_session

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import nomad_create as nc  # noqa: E402

results, sent = [], []


def check(name, ok, detail: object = ""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"\n     {str(detail)[:400]}" if detail and not ok else ""), flush=True)


PROFILES = {"ok": True, "profiles": [
    {"id": "game", "title": "Game", "description": "A game.",
     "interview": {"ask": [{"id": "engine", "question": "Which engine?", "kind": "single",
                            "options": ["Unity", "Godot", "not decided yet"]}]}},
    {"id": "generic", "title": "Generic", "description": "Anything else."}]}
ANSWER = {"POST /create-project": {"ok": True, "id": "tide-log", "stage": "setup", "steps": [],
                                   "running": True, "lines": ["Setting up **Tide Log**"]}}


def fake_request(method, url, json=None, params=None, timeout=None, headers=None):
    path = url.split("8090", 1)[1]
    sent.append((method, path, json, params, headers))

    class R:
        def json(self):
            if path == "/profiles":
                return PROFILES
            return ANSWER.get(f"{method} {path}", {"ok": True, "id": params and params.get("id"),
                                                   "stage": "done", "steps": ["repo"],
                                                   "running": False, "lines": []})
    return R()


async def call(session, tool, **args):
    r = await session.call_tool(tool, args)
    return r, "\n".join(c.text for c in r.content if hasattr(c, "text"))


async def main():
    tools = {t.name: t for t in await nc.mcp.list_tools()}
    check("three tools", set(tools) == {"project_types", "create_project", "create_project_status"}, sorted(tools))
    check("create_project makes things, is idempotent (a re-call resumes), is not destructive",
          tools["create_project"].annotations.readOnlyHint is False
          and tools["create_project"].annotations.idempotentHint is True
          and tools["create_project"].annotations.destructiveHint is False)
    check("the other two only read", all(tools[n].annotations.readOnlyHint for n in ("project_types", "create_project_status")))
    check("create_project requires name and prd", set(tools["create_project"].inputSchema["required"]) == {"name", "prd"},
          tools["create_project"].inputSchema.get("required"))
    check("its description says not to make parts by hand",
          "instead of making any of those parts" in tools["create_project"].description)

    nc.requests.request = fake_request
    nc._token = lambda: "disp-t"
    async with create_connected_server_and_client_session(nc.mcp._mcp_server) as s:
        r, text = await call(s, "project_types")
        check("project_types lists each type's own questions", not r.isError and "engine (single: Unity" in text
              and r.structuredContent["types"][0]["questions"][0]["id"] == "engine", text)

        sent.clear()
        r, text = await call(s, "create_project", name="Tide Log", prd="# PRD: Tide Log", type="game",
                             extra={"engine": "Unity"}, autopilot=True)
        m, path, body, _, hdr = sent[-1]
        check("create_project posts every answer to the dispatcher with its token",
              (m, path, hdr) == ("POST", "/create-project", {"X-Nomad-Token": "disp-t"})
              and body["type"] == "game" and body["extra"] == {"engine": "Unity"} and body["autopilot"] is True
              and body["visibility"] == "private", sent[-1])
        check("it says the run started and how to follow it", not r.isError and "`tide-log`: setup, running" in text
              and "create_project_status" in text and r.structuredContent["running"] is True, text)

        sent.clear()
        r, _ = await call(s, "create_project", name="Tide Log", prd="")
        check("an empty PRD is refused before the dispatcher", r.isError and not sent)
        r, _ = await call(s, "create_project", name="Tide Log", prd="x", visibility="secret")
        check("visibility is private or public", r.isError and not sent)

        ANSWER["POST /create-project"] = {"ok": False, "id": "nomad", "error": "name: `nomad` is already a project."}
        r, text = await call(s, "create_project", name="Nomad", prd="x")
        check("a refusal is an error result naming why", r.isError and "already a project" in text, text)

        r, text = await call(s, "create_project_status", id="tide-log")
        check("create_project_status asks with the id", not r.isError and sent[-1][1:4:2] == ("/create-project", {"id": "tide-log"})
              and "done: repo" in text, (sent[-1], text))
        r, _ = await call(s, "create_project_status", id="../etc")
        check("a bad id is refused before the dispatcher", r.isError and sent[-1][3] != {"id": "../etc"})

    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
