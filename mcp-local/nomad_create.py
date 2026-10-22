"""NOMAD project creation (MCP): the one way an agent sets a new project up (#1374).

A new project is many parts: the repo and its first commit, the projects.yaml entry with its
type, policies and gates, the nomad-pm board, the roadmap, the Zulip stream, the mission-control
row. An agent never makes those by hand. It calls `create_project`, which runs
nomad_projects/create.py, the module the Open WebUI + New project chat runs, through the
dispatcher's POST /create-project and the engine. The same answers give the same project.

A new project is Large work (docs/SDLC.md): its PRD needs the operator's OK first. So
`create_project` takes the approved PRD, and the call itself is one the operator approves: never
put it on an allow-list.

Tools:
  project_types()               the types, each with its own questions (for `extra`)
  create_project(name, prd, …)  start, or resume, setting a project up
  create_project_status(id)     how that run is going
"""
import os
from typing import Annotated, Optional

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

DISPATCH_URL = os.environ.get("NOMAD_DISPATCH_URL", "http://127.0.0.1:8090").rstrip("/")
TOKEN_FILE = os.path.expanduser(os.environ.get("NOMAD_DISPATCH_TOKEN_FILE",
                                               "~/.config/nomad/dispatch-token"))
READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
# It makes things, and a second call with the same answers resumes rather than doubling them.
MAKE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                       openWorldHint=False)


def _token() -> str:
    tok = os.environ.get("NOMAD_DISPATCH_TOKEN", "").strip()
    if not tok and os.path.isfile(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    return tok


def _call(method: str, path: str, body=None, params=None) -> dict:
    try:
        r = requests.request(method, DISPATCH_URL + path, json=body, params=params, timeout=90,
                             headers={"X-Nomad-Token": _token()})
        got = r.json()
        return got if isinstance(got, dict) else {"ok": False, "error": "not a JSON object"}
    except Exception as e:
        return {"ok": False, "error": f"the dispatcher can't be reached: {type(e).__name__}"}


class Question(BaseModel):
    id: str = Field(description="The key in create_project's `extra`.")
    question: str
    kind: str = Field(description="single, multi or text.")
    options: list[str] = []


class ProjectType(BaseModel):
    id: str
    title: str
    description: str = ""
    questions: list[Question] = Field(description="The type's own interview questions.")


class Types(BaseModel):
    types: list[ProjectType]


class Run(BaseModel):
    ok: bool
    id: str = ""
    stage: str = Field("", description="setup while steps remain, done when it is set up.")
    steps: list[str] = Field([], description="Steps done: repo, registry, board, roadmap, "
                                             "stream, console, refresh.")
    running: bool = False
    lines: list[str] = Field([], description="What each step reported, since the engine started.")
    error: str = ""


def _both(text: str, data: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structuredContent=data)


def _run(got: dict) -> CallToolResult:
    run = Run(**{k: got[k] for k in Run.model_fields if k in got and got[k] is not None})
    if not run.ok:
        return CallToolResult(content=[TextContent(type="text", text=f"Not created: {run.error}")],
                              structuredContent=run.model_dump(), isError=True)
    head = (f"`{run.id}`: {run.stage or '?'}" + (", running" if run.running else "")
            + (f"; done: {', '.join(run.steps)}" if run.steps else ""))
    tail = "\n".join(run.lines[-10:])
    if run.running:
        tail += "\nCall create_project_status to follow it; the board step can take 15 minutes."
    elif run.stage == "setup":
        tail += "\nA step failed. Fix the cause, then call create_project again: it resumes there."
    return _both(head + ("\n" + tail.strip() if tail.strip() else ""), run.model_dump())


mcp = FastMCP("nomad-create")


@mcp.tool(title="List project types", annotations=READ)
def project_types() -> Annotated[CallToolResult, Types]:
    """The project types create_project takes as `type`, each with its own questions. Answer
    those in `extra`, by question id. `generic` fits anything else."""
    got = _call("GET", "/profiles")
    if not got.get("ok"):
        return CallToolResult(content=[TextContent(type="text", text=str(got.get("error")))],
                              isError=True)
    types = [ProjectType(id=p["id"], title=p.get("title") or p["id"],
                         description=p.get("description") or "",
                         questions=[Question(id=q["id"], question=q.get("question") or "",
                                             kind=q.get("kind") or "text",
                                             options=list(q.get("options") or ()))
                                    for q in (p.get("interview") or {}).get("ask") or ()])
             for p in got.get("profiles") or ()]
    text = "\n".join(f"- {t.id}: {t.description}" + "".join(
        f"\n    {q.id} ({q.kind}{': ' + ', '.join(q.options) if q.options else ''}): {q.question}"
        for q in t.questions) for t in types)
    return _both(text, Types(types=types).model_dump())


@mcp.tool(title="Create a project", annotations=MAKE)
def create_project(
    name: Annotated[str, Field(description="The project's name; its slug becomes the id.",
                               min_length=2, max_length=80)],
    prd: Annotated[str, Field(description="The PRD the operator approved, in markdown. Never "
                                          "one they have not seen.", min_length=1)],
    type: Annotated[str, Field(description="A type id from project_types.")] = "generic",
    pitch: Annotated[str, Field(description="What the project is, in a sentence or two.")] = "",
    problem: Annotated[str, Field(description="What problem it solves, and why now.")] = "",
    users: Annotated[str, Field(description="Who it is for.")] = "",
    mvp: Annotated[str, Field(description="The smallest version worth shipping.")] = "",
    stack: Annotated[str, Field(description="Language, framework, where it runs. Empty: the "
                                            "type's defaults.")] = "",
    visibility: Annotated[str, Field(description="private or public.",
                                     pattern="^(private|public)$")] = "private",
    local_only: Annotated[bool, Field(description="Its chats use local models only.")] = False,
    autopilot: Annotated[bool, Field(description="Give it an autopilot block. The switch "
                                                 "stays off until the operator turns it on.")] = False,
    new_type: Annotated[str, Field(description="When no type fits: what it is. It is set up "
                                               "as generic and flagged for a new type.")] = "",
    extra: Annotated[Optional[dict[str, str | list[str]]],
                     Field(description="Answers to the type's own questions, by id.")] = None,
) -> Annotated[CallToolResult, Run]:
    """Set a new project up end to end: repo, registry entry (type, policies, gates), nomad-pm
    board, roadmap, Zulip stream and mission-control row, the same as the Open WebUI
    + New project chat makes for the same answers. Use it instead of making any of those parts
    yourself. It needs the PRD the operator approved. The steps run in the background: the
    result says what started, and create_project_status follows it. Calling it again with the
    same name resumes a run that stopped at a failed step, and reports one that is done.
    Nothing is pushed and no GitHub remote is made."""
    body = {"name": name, "prd": prd, "type": type, "pitch": pitch, "problem": problem,
            "users": users, "mvp": mvp, "stack": stack, "visibility": visibility,
            "local_only": local_only, "autopilot": autopilot, "new_type": new_type,
            "extra": extra or {}}
    return _run(_call("POST", "/create-project", body))


@mcp.tool(title="Follow a project's setup", annotations=READ)
def create_project_status(
    id: Annotated[str, Field(description="The project id create_project returned.",
                             pattern="^[a-z0-9][a-z0-9-]{1,39}$")],
) -> Annotated[CallToolResult, Run]:
    """How a create_project run is going: its stage, the steps done, and what each reported."""
    return _run(_call("GET", "/create-project", params={"id": id}))


if __name__ == "__main__":
    mcp.run()
