"""The tools an Open WebUI chat can call (#1146, docs/prd/owui-sessions.md §6.3).

Separate from the cockpit's CHAT_TOOL_ALLOWLIST: the cockpit only reads, and an Open WebUI chat
can also act, under the same SDLC tiers an agent works under.

| tier  | tools                                                      | what happens                  |
|-------|------------------------------------------------------------|-------------------------------|
| read  | the cockpit's read-only tools (chat_tools, chat_pm_tools)  | as in the cockpit             |
| green | pm_create, pm_comment, pm_transition, pm_update            | done at once; the reply links |
| gated | build_task, run_next_task, capture                         | a run that stops at the gate  |

Red actions (send, publish, email, post) have no tool: they go through `capture`, whose run
stops at the gate like any other. Delete, push and spend aren't reachable at all. A gated tool
can only start a run (`Actions.capture`, `run_task`, `run_next`); nothing here can resume one,
so approving is the operator's: the cockpit, Zulip, or typing `approve` in the chat, which
owui_chat sends to the gate handler, not to a tool.

`build(actions)` returns `{name: {"tier", "description", "input_schema", "call"}}`, the shape
runtime.run_tools takes; `openai(tools)` is the same list for LiteLLM tool calling. `call(args)`
returns a dict and never raises.
"""
import os
from dataclasses import dataclass
from typing import Callable

import chat_pm_tools
import chat_tools

PUBLIC_URL = os.environ.get("NOMAD_PM_PUBLIC_URL", "http://localhost:8097").rstrip("/")

# The cockpit's read-only tools, by name (server.CHAT_TOOL_ALLOWLIST). A list of what is
# allowed: a tool added to those modules later stays out of reach until it's named here.
READS = ("pipeline_runs", "pipeline_run_detail", "approval_queue", "autopilot_runs",
         "autopilot_status", "autopilot_config", "system_health",
         "pm_search", "pm_get", "pm_tree", "pm_ready", "pm_project_summary", "pm_my_queue")
GREEN = ("pm_create", "pm_comment", "pm_transition", "pm_update")
GATED = ("build_task", "run_next_task", "capture")
CREATE_TYPES = ("Task", "Bug", "Epic", "User story")
GATE_NOTE = ("Nothing has run. It waits at the gate: approve it in the cockpit, in Zulip, or "
             "by saying approve here.")


@dataclass
class Actions:
    """What the tools act through, one per turn. `board` is the chat's OpenProject project
    (the registry row's `board`), or "" for General."""
    board: str
    op: Callable                    # (board) -> pm_openproject.OpenProject
    capture: Callable               # (goal) -> engine.start_run state
    run_task: Callable              # (task id) -> engine.run_task state
    run_next: Callable              # (board) -> engine.run_next_task state, picked on that board


def link(wp_id) -> str:
    return f"{PUBLIC_URL}/work_packages/{int(wp_id)}"


def _wp_id(v) -> int:
    return int(str(v).strip().lstrip("#"))


def _brief(wp: dict) -> dict:
    links = wp.get("_links") or {}
    return {"id": wp.get("id"), "subject": wp.get("subject"),
            "type": (links.get("type") or {}).get("title"),
            "status": (links.get("status") or {}).get("title"),
            "project": (links.get("project") or {}).get("title"),
            "url": link(wp["id"]) if wp.get("id") else ""}


def _board_of(op, wp_id) -> str:
    """The OpenProject project a work package sits on, as an id OpenProject(project=…) takes."""
    href = ((op.get(wp_id).get("_links") or {}).get("project") or {}).get("href") or ""
    return href.rstrip("/").rsplit("/", 1)[-1]


# ── green: nomad-pm writes ────────────────────────────────────────────────────────────────

def pm_create(a: Actions, type, subject, parent="", description="", lane="dev"):
    if type not in CREATE_TYPES:
        return {"error": f"type must be one of {', '.join(CREATE_TYPES)}"}
    board = a.board
    if parent:
        board = _board_of(a.op(a.board or None), _wp_id(parent))     # the parent's board wins
    if not board:
        return {"error": "this is the General chat, so name a parent (an Epic or Feature id) "
                         "to say which board the item goes on"}
    wp = a.op(board).create(type, subject, parent=_wp_id(parent) if parent else None,
                            lane=lane or "dev", description=description)
    return {"created": _brief(wp)}


def pm_comment(a: Actions, id, text):
    a.op(a.board or None).comment(_wp_id(id), text)
    return {"commented": {"id": _wp_id(id), "url": link(id)}}


def pm_transition(a: Actions, id, status, comment=""):
    wp = a.op(a.board or None).move(_wp_id(id), status, comment)
    return {"moved": _brief(wp)}


def pm_update(a: Actions, id, subject="", description=""):
    if not subject and not description:
        return {"error": "give a subject or a description to change"}
    op = a.op(a.board or None)
    wp = op.get(_wp_id(id))
    body = {"lockVersion": wp["lockVersion"]}
    if subject:
        body["subject"] = subject[:255]
    if description:
        body["description"] = {"raw": description}
    return {"updated": _brief(op.call("PATCH", f"/work_packages/{wp['id']}", body))}


# ── gated: each one only starts a run that waits at the gate ─────────────────────────────

def _gated(st: dict) -> dict:
    if not isinstance(st, dict) or st.get("error") or st.get("done"):
        return {"error": (st or {}).get("error") or (st or {}).get("note") or "nothing started"}
    p = st.get("proposal") or {}
    out = {"run_id": st.get("run_id"), "status": st.get("status"), "lane": st.get("lane"),
           "action": p.get("action"), "target": p.get("target"), "note": GATE_NOTE}
    if st.get("task_id"):
        out.update(task_id=st["task_id"], task_url=link(st["task_id"]))
    return out


def build_task(a: Actions, id):
    return _gated(a.run_task(_wp_id(id)))


def run_next_task(a: Actions):
    if not a.board:
        return {"error": "this is the General chat: pick a project to take its next task"}
    return _gated(a.run_next(a.board))


def capture(a: Actions, goal):
    return _gated(a.capture(goal))


_ID = {"type": "string", "description": "work package id, e.g. 944 or #944"}
SPECS = {
    "pm_create": (pm_create, "Create a nomad-pm work package (Task, Bug, Epic or User story) on "
                  "this chat's board, or under `parent` on the parent's board. Returns its id and "
                  "link; give the operator both.", {
                      "type": {"type": "string", "enum": list(CREATE_TYPES)},
                      "subject": {"type": "string"},
                      "parent": {**_ID, "description": "the Epic or Feature it goes under"},
                      "description": {"type": "string", "description": "markdown"},
                      "lane": {"type": "string",
                               "description": "dev (default), comms, research, support, ads"}},
                  ("type", "subject")),
    "pm_comment": (pm_comment, "Post a comment on a work package.",
                   {"id": _ID, "text": {"type": "string"}}, ("id", "text")),
    "pm_transition": (pm_transition, "Move a work package to another status (Ready, In Progress, "
                      "Blocked, Done, Won't do…), one legal step at a time. `comment` goes on with "
                      "the move.", {"id": _ID, "status": {"type": "string"},
                                    "comment": {"type": "string"}}, ("id", "status")),
    "pm_update": (pm_update, "Change a work package's subject or description.",
                  {"id": _ID, "subject": {"type": "string"},
                   "description": {"type": "string", "description": "markdown, replaces the old"}},
                  ("id",)),
    "build_task": (build_task, "Build one Task or Bug: starts a run for it that waits at the "
                   "operator's gate. Nothing is built until they approve.", {"id": _ID}, ("id",)),
    "run_next_task": (run_next_task, "Take this project's next Ready task and start its run, "
                      "which waits at the operator's gate.", {}, ()),
    "capture": (capture, "Anything else that acts outside nomad-pm (send, publish, email, post, "
                "research, a build with no task): describe it as a goal. The pipeline routes it "
                "and the run waits at the operator's gate.",
                {"goal": {"type": "string", "description": "what should be done, in a sentence"}},
                ("goal",)),
}


def _call(fn, a: Actions, name: str, schema: dict, args) -> dict:
    if not isinstance(args, dict):
        return {"error": "arguments must be an object"}
    extra = sorted(set(args) - set(schema["properties"]))
    if extra:
        return {"error": f"{name} takes no argument {', '.join(extra)}"}
    missing = [k for k in schema.get("required", []) if not args.get(k)]
    if missing:
        return {"error": f"{name} needs {', '.join(missing)}"}
    try:
        return fn(a, **args)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}


def build(a: Actions) -> dict:
    out = {}
    for mod in (chat_tools, chat_pm_tools):
        for name, t in mod.TOOLS.items():
            if name in READS and t.get("read_only") is True:
                out[name] = {"tier": "read", "description": t["description"],
                             "input_schema": t["input_schema"],
                             "call": lambda args, _m=mod, _n=name: _m.call(_n, args)}
    for name, (fn, desc, props, required) in SPECS.items():
        schema = {"type": "object", "additionalProperties": False, "properties": props,
                  "required": list(required)}
        out[name] = {"tier": "green" if name in GREEN else "gated", "description": desc,
                     "input_schema": schema,
                     "call": lambda args, _f=fn, _n=name, _s=schema: _call(_f, a, _n, _s, args)}
    return out


def openai(tools: dict) -> list:
    """The tools in OpenAI's chat-completions format, for LiteLLM."""
    return [{"type": "function", "function": {"name": n, "description": t["description"],
                                              "parameters": t["input_schema"]}}
            for n, t in tools.items()]
