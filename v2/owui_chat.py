"""One Open WebUI turn (#1117, docs/prd/owui-sessions.md §6.2).

The pipe posts {entry, chat_id, messages, files}; `turn()` yields events the pipe turns into
Open WebUI output, one JSON object per line on the wire. NOMAD Local (#1436) adds
`local_only: true`, which makes any entry local-only (it never loosens one), and `local_loaded`,
the local alias already on the GPU, which such a chat then stays on:

    {"type": "status", "text": "routing…"}         a status line, not part of the answer
    {"type": "delta", "text": "..."}               answer text, in order
    {"type": "done", "routed": ..., "served": ...} last event, with what the turn recorded

Per turn: route (owui_router), load the project context at the chosen model's budget
(owui_context), recall memory, stream the answer through LiteLLM, end with the footer
`routed: X (why: …) · served by: Y`, remember both sides under `owui:<chat_id>`, log the turn.

A chat is bound to its entry on its first turn (§6.5): a later turn naming another entry gets the
bound one, so the Project dropdown's lock doesn't rest on the browser. The `done` event carries the
bound entry. `context()` gives the same project context to the global filter, for chats answered
by a model the operator picked instead of NOMAD Auto.

Local-only chats (`local_only: true` projects, and any project chat while the registry can't be
read) send the router's local fallback list with the request, replacing LiteLLM's chain,
so a failing local model never falls through to the cloud (N3). Every outside call is a Deps
field, so the tests run it against a fake LiteLLM.

Tools (M2, §6.3, #1147). A turn the router marks `needs_tools`, or one that reads like an action
("make a bug…", "build that fix", "#944"), gets the chat's owui_tools: reads, green nomad-pm
writes, and gated tools that only start a run at the gate. Local models (always, for a local-only
chat) call them through LiteLLM's tool calling; cloud models through runtime.run_tools on the Agent
SDK, because the claude-bridge behind `balanced` and `deep` ignores OpenAI tools. When the SDK
can't run, the turn falls back to a local model, never to an answer that pretends.

A message that starts with approve or reject (intents.classify's gate_decision) goes to the gate
handler the cockpit's voice uses, before any model: the first approve shows the proposal, a
second one approves that exact version.
"""
import dataclasses
import json
import re
import time
from dataclasses import dataclass
from typing import Callable

import intents
import owui_context
import owui_delete
import owui_newproject
import owui_router
import owui_tools

HISTORY_TURNS = 12                  # earlier messages sent with the turn
HISTORY_CHARS = 2000                # each clipped to this
SESSION_PREFIX = "owui:"

SYSTEM = ("You are NOMAD, the operator's assistant for their projects, answering in Open WebUI. "
          "Answer concisely and concretely. When you cite a task, use its #id. This turn has no "
          "tools: answer from the context below, say when it doesn't cover the question, and never "
          "claim you created, deleted, pushed, sent, ran or changed something, and never write step "
          "lists (\"✓ 1/5 …\") or a \"routed:\" footer: the engine adds those. Say what you "
          "would do and that nothing was done. If the operator wants something done, ask them to "
          "say it as an instruction (\"create a task for…\", \"build #123\").")
TOOLS_SYSTEM = (
    "You are NOMAD, the operator's assistant for their projects, answering in Open WebUI, with "
    "tools. Look things up rather than guess. nomad-pm writes (pm_create, pm_comment, "
    "pm_transition, pm_update) happen at once: give the #id and the link of what you created or "
    "changed. Building a task (build_task), taking the next task (run_next_task) and anything "
    "that acts outside nomad-pm, such as sending, publishing or research (capture), only start a "
    "run that waits at the operator's gate: say so, give the run id, and tell them to say "
    "approve. Never say something ran, was sent or was built when it waits at the gate. A tool "
    "that answers with an error didn't do anything: say what failed. When an earlier message "
    "names the item (\"that fix\"), use its #id from there. Only claim an action a tool call in "
    "this turn did. For anything no tool does (deleting or archiving a project, pushing, "
    "deploying), say you can't do it from here and nothing was done. Never write step lists "
    "(\"✓ 1/5 …\") or a \"routed:\" footer: the engine adds those.")
ACTION_HINT = re.compile(
    r"#\d+|\b(create|make|add|open|file|log|build|run|start|dispatch|capture|comment|move|"
    r"close|mark|update|rename|assign|send|publish|email|post|research|next task|delete|remove|"
    r"archive|push|deploy|merge)\b", re.I)
DELETE_ASK = re.compile(           # #1198: a delete in other words gets the command, not a model
    r"^\W*(please\s+)?(delete|remove|archive|drop|erase|get rid of)\b.*(\bproject\b|#\d+)", re.I | re.S)
DELETE_HELP = ("Nothing was deleted. A chat model can't delete anything. To delete a whole project, "
               "send `delete project <id>` (in a chat bound to it, `delete project` is enough): it "
               "lists what will go and waits for your approval. Work items can't be deleted from a "
               "chat; close them as Won't do instead.")
TOOL_LOCAL = ("fast", "private", "code")      # the local aliases that do tool calls (2026-10-12)
TOOL_CLOUD = ("balanced", "deep")             # runtime._SDK_MODEL
TOOL_STEPS = 8
TOOL_RESULT_CHARS = 6000
GENERAL = "This is the General chat: no project is selected."
FILTER = ("NOMAD project context for this chat (the operator picked the project in Open WebUI). "
          "Use it to answer; say when it doesn't cover the question. When you cite a task, use "
          "its #id.")


# Lines a model writes that look like the engine's own output (#1198): setup step lines and the
# routing footer. Only the engine writes these; owui_setup's real ✓ lines don't pass through here.
NOT_RUN = "(the model claimed steps that never ran; nothing was done)"
_STEP_LINE = re.compile(r"^[\s>*_`#-]*(?:✓|✔|✅|⚠️?|❌)[\s*_]*\d+\s*/\s*\d+\b")
_FOOT_LINE = re.compile(r"^[\s>*_`#-]*routed\s*:|served by\s*:", re.I)


def scrub(text: str, called: bool = False) -> str:
    """`text` without lines that impersonate engine output: footer lines always, `✓ n/m` step
    lines when no tool ran. A run of them becomes one note."""
    g = LineGuard(called)
    return g.feed(text or "") + g.flush()


class LineGuard:
    """scrub() for a stream: holds back the unfinished line until its newline arrives."""

    def __init__(self, called: bool = False):
        self.called, self.held, self.noted = called, "", False

    def _line(self, line: str) -> str:
        if _FOOT_LINE.search(line) or (not self.called and _STEP_LINE.match(line)):
            note, self.noted = ("" if self.noted else NOT_RUN + "\n"), True
            return note
        if line.strip():
            self.noted = False
        return line + "\n"

    def feed(self, chunk: str) -> str:
        *done, self.held = (self.held + chunk).split("\n")
        return "".join(self._line(x) for x in done)

    def flush(self) -> str:
        rest, self.held = self.held, ""
        return self._line(rest).removesuffix("\n") if rest else ""


@dataclass
class Deps:
    registry: Callable              # () -> [detail rows] | None
    fetch_context: Callable         # (project_id) -> dispatcher /project-context dict
    search: Callable                # chat_pm_tools.pm_search
    complete: Callable              # (alias, system, user, timeout) -> text, for the router
    stream: Callable                # (body) -> (headers, iterator of text deltas)
    recall: Callable                # (query, exclude_session) -> [{role, content}]
    remember: Callable              # (session, role, content)
    log: Callable                   # (row dict)
    now: Callable = time.monotonic
    bound: Callable = lambda chat_id: None          # the chat's entry, if it has one
    bind: Callable = lambda chat_id, entry: None    # record the chat's entry
    tools: Callable = lambda info: {}               # (entry info) -> owui_tools.build(...) dict
    complete_tools: Callable | None = None          # (body) -> (headers, assistant message)
    run_tools: Callable | None = None               # runtime.run_tools; raises when it can't run
    gate: Callable | None = None                    # (decision, all) -> reply text
    np_load: Callable = lambda chat_id: None        # + New project state for the chat, or None
    np_save: Callable = lambda chat_id, state: None
    draft: Callable | None = None                   # (alias, system, user) -> long text, the PRD
    setup: Callable | None = None                   # (state, save, chat_id) -> progress lines (#1153)
    profiles: Callable | None = None                # () -> the project types, [] when unreadable (#1373)
    delete: Callable | None = None                  # (text, bound project, chat_id) -> reply pieces (#1200)


def text_of(content) -> tuple[str, bool]:
    """(text, has_image) for an OpenAI-style message content, a string or a list of parts."""
    if isinstance(content, str):
        return content, False
    text, image = [], False
    for part in content or []:
        if isinstance(part, dict):
            if part.get("type") == "text":
                text.append(part.get("text") or "")
            elif part.get("type") == "image_url":
                image = True
    return "\n".join(text), image


def entry_info(entry: str, rows) -> dict:
    """What the dropdown entry is. A project chat whose registry row can't be read stays local:
    without the row there's no way to know it isn't `local_only`."""
    if entry == "general":
        return {"kind": "general", "project": None, "name": "General", "local_only": False}
    if entry == "new-project":
        return {"kind": "new", "project": None, "name": "+ New project", "local_only": False}
    if entry.startswith("p-"):
        pid = entry[2:]
        if rows is None:
            return {"kind": "project", "project": pid, "name": pid, "local_only": True,
                    "note": "registry unreachable, so this turn stays local"}
        row = next((r for r in rows if r.get("id") == pid), None)
        if row:
            return {"kind": "project", "project": pid, "name": row.get("name") or pid,
                    "local_only": row.get("local_only") is True, "board": row.get("board") or ""}
    return {"kind": "unknown", "project": None, "name": entry, "local_only": True}


def _info(entry: str, deps: Deps) -> dict:
    return entry_info(entry, deps.registry() if entry.startswith("p-") else [])


def resolve(entry: str, chat_id: str, deps: Deps) -> tuple[str, dict, list]:
    """(entry, info, notes) for the chat: its bound entry, binding it on the first turn. An
    unknown entry is never bound. If the binding store can't be read, the turn uses the entry it
    was given and says so."""
    info = _info(entry, deps)
    if info["kind"] == "unknown" or not chat_id:
        return entry, info, [n for n in (info.get("note"),) if n]
    bound, note = _bind(entry, chat_id, deps)
    if bound != entry:
        info = _info(bound, deps)
    return bound, info, [n for n in (note, info.get("note")) if n]


def _bind(entry: str, chat_id: str, deps: Deps) -> tuple[str, str]:
    try:
        have = deps.bound(chat_id)
    except Exception:
        return entry, "project lock unavailable"
    if have and have != entry:
        return have, f"this chat stays on {have}"
    if not have:
        try:
            deps.bind(chat_id, entry)
        except Exception:
            return entry, "project lock unavailable"
    return entry, ""


def footer(d: owui_router.Decision, headers: dict, notes: list) -> str:
    served = headers.get("x-litellm-model-group") or "nothing"
    tried = str(headers.get("x-litellm-attempted-fallbacks") or "0")
    line = f"routed: {d.model} (why: {d.reason}) · served by: {served}"
    if served not in ("nothing", d.model) or tried != "0":
        line += f" (fallback after {d.model} failed)"
    extra = [n for n in [d.note] + notes if n]
    return line + (" · " + " · ".join(extra) if extra else "")


def _body(d: owui_router.Decision, messages: list) -> dict:
    assert d.model not in owui_router.PAID
    body = {"model": d.model, "messages": messages, "stream": True}
    if d.fallbacks is not None:     # local-only: our list replaces LiteLLM's chain
        if d.fallbacks:
            body["fallbacks"] = list(d.fallbacks)
        else:
            body["disable_fallbacks"] = True
    return body


def _task(msgs: list, deps: Deps):
    """A background task from Open WebUI (title, tags, follow-ups). Its prompt is in `msgs`
    already; it gets `fast` with a local fallback, and no context, footer, memory or log."""
    body = {"model": "fast", "fallbacks": ["private"], "stream": True,
            "messages": [{"role": m.get("role"), "content": text_of(m.get("content"))[0]}
                         for m in msgs]}
    try:
        _, chunks = deps.stream(body)
        text = "".join(c for c in chunks if c)
    except Exception:
        text = ""
    yield {"type": "delta", "text": text}
    yield {"type": "done", "routed": "fast", "served": None}


def _transcript(messages: list) -> str:
    """History and the question as one prompt, for runtime.run_tools (it takes a single user
    message)."""
    *earlier, last = messages
    if not earlier:
        return str(last["content"])
    lines = "\n".join(f"{m['role']}: {m['content']}" for m in earlier)
    return f"Earlier in this chat:\n{lines}\n\nNow the operator says:\n{last['content']}"


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", text or "", flags=re.S).strip()


def _tool_plan(d: owui_router.Decision, info: dict, deps: Deps,
               notes: list) -> tuple[owui_router.Decision, bool]:
    """(the decision with the model that will run the tools, whether it's the Agent SDK). Chosen
    before the context loads, so the context fits that model's window."""
    if not info["local_only"] and d.model not in owui_router.LOCAL and deps.run_tools:
        alias, cloud = (d.model if d.model in TOOL_CLOUD else "balanced"), True
    else:
        alias, cloud = (d.model if d.model in TOOL_LOCAL else "private"), False
    if alias != d.model:
        notes.append(f"{d.model} can't call tools here, so {alias} answered")
        d = dataclasses.replace(d, model=alias)
    return d, cloud


def _tool_room(alias: str, tools: dict) -> int:
    """Tokens a local tool turn's messages may take: the window less the answer and the tool
    schemas, with a margin because 4 characters a token is only an estimate."""
    schema = owui_router.tokens(json.dumps(owui_tools.openai(tools)))
    return int((owui_router.WINDOW.get(alias, 8192) - owui_router.REPLY_RESERVE - schema) * 0.8)


def tool_context_budget(alias: str, tools: dict) -> int:
    return max(500, min(owui_router.context_budget(alias), _tool_room(alias, tools) // 3))


def _fit(msgs: list, room: int) -> list:
    """The messages cut to `room` tokens: the oldest history first, then the system prompt's
    tail. The question and everything after it stay whole."""
    size = lambda ms: sum(owui_router.tokens(str(m.get("content") or "")) + 8 for m in ms)
    msgs = list(msgs)
    question = max(i for i, m in enumerate(msgs) if m.get("role") == "user")
    while size(msgs) > room and question > 1:      # msgs[1:question] is the history
        msgs.pop(1)
        question -= 1
    # Tool results pile up each step. Ollama silently drops the oldest messages of an over-long
    # prompt, which can leave a tool message without its assistant call and fail the template
    # ("A tool message must follow an assistant or tool message"), so shrink the oldest results
    # here, keeping every pair whole.
    for i in range(question + 1, len(msgs)):
        if size(msgs) <= room:
            break
        if msgs[i].get("role") == "tool" and len(str(msgs[i].get("content") or "")) > 300:
            text = str(msgs[i]["content"])
            cut = max(300, len(text) - (size(msgs) - room) * 4)
            msgs[i] = {**msgs[i], "content": text[:cut] + "…(cut to fit)"}
    over = size(msgs) - room
    if over > 0 and msgs[0].get("role") == "system":
        text = msgs[0]["content"]
        keep = max(400, len(text) - over * 4)
        msgs[0] = {**msgs[0], "content": text[:keep] + "\n…(context cut to fit the model)"}
    return msgs


def _tool_turn(d: owui_router.Decision, cloud: bool, messages: list, tools: dict, deps: Deps,
               notes: list):
    """Answer with tools. Yields status and delta events; returns (decision as served, headers,
    answer parts, error, tool names called)."""
    called = []
    if cloud:
        alias = d.model
        yield {"type": "status", "text": f"→ {alias} with tools"}
        try:
            res = deps.run_tools(messages[0]["content"], _transcript(messages[1:]), tools, alias)
        except Exception as e:
            notes.append(f"Agent SDK unavailable ({type(e).__name__}), so private answered")
            d = dataclasses.replace(d, model="private")
        else:
            called = [c.get("name") for c in res.get("tool_calls") or []]
            reply = res.get("reply") or ""
            if res.get("stopped"):
                reply += ("\n\n" if reply else "") + (
                    f"(I stopped at the {'step' if res['stopped'] == 'turns' else 'time'} limit, "
                    "so this may be incomplete.)")
            reply = scrub(reply, bool(called)) or "⚠ The model finished without an answer."
            yield {"type": "delta", "text": reply}
            return d, {"x-litellm-model-group": alias}, [reply], "", called

    alias, ld = d.model, d
    room = _tool_room(alias, tools)
    result_chars = max(1500, min(TOOL_RESULT_CHARS, room))
    yield {"type": "status", "text": f"→ {alias} with tools"}
    body = {"model": alias, "stream": False, "tools": owui_tools.openai(tools)}
    if d.fallbacks is not None:                     # local-only: never let a tool turn leave the box
        chain = owui_router.LOCAL_FALLBACKS.get(alias) or []
        body.update({"fallbacks": chain} if chain else {"disable_fallbacks": True})
    msgs, headers, answer, msg = list(messages), {}, "", {}
    try:
        for _ in range(TOOL_STEPS):
            headers, msg = deps.complete_tools({**body, "messages": _fit(msgs, room)})
            headers = {k.lower(): v for k, v in (headers or {}).items()}
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = _strip_think(msg.get("content") or "")
                break
            msgs.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                name = fn.get("name") or ""
                called.append(name)
                yield {"type": "status", "text": f"🔧 {name}"}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = None
                res = (tools[name]["call"](args if args is not None else "bad json")
                       if name in tools else {"error": f"no tool {name!r}"})
                msgs.append({"role": "tool", "tool_call_id": c.get("id") or name,
                             "content": json.dumps(res, default=str)[:result_chars]})
        else:
            answer = (_strip_think(msg.get("content") or "") + "\n\n" if msg.get("content") else ""
                      ) + f"(I stopped after {TOOL_STEPS} tool steps, so this may be incomplete.)"
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"
        msg = f"⚠ No model answered ({error})."
        yield {"type": "delta", "text": msg}
        return ld, headers, [msg], error, called
    answer = scrub(answer, bool(called)) or "⚠ The model finished without an answer."
    yield {"type": "delta", "text": answer}
    return ld, headers, [answer], "", called


def _gate_turn(decision: dict, text: str, entry: str, info: dict, chat_id: str, session: str,
               deps: Deps, t0: float):
    """approve / reject typed in the chat: the cockpit voice's gate handler, no model."""
    try:
        reply = deps.gate(decision.get("decision", "approved"), bool(decision.get("all")))
    except Exception as e:
        reply = f"⚠ The gate couldn't be reached ({type(e).__name__}: {str(e)[:150]}). Nothing changed."
    foot = "gate decision · no model"
    yield {"type": "delta", "text": f"{reply}\n\n---\n{foot}"}
    for role, content in (("user", text), ("assistant", reply)):
        try:
            deps.remember(session, role, content)
        except Exception:
            pass
    try:
        deps.log({"chat_id": chat_id, "entry": entry, "project": info["project"] or "",
                  "local_only": info["local_only"], "routed": "gate", "routed_by": "rule",
                  "reason": decision.get("decision", ""), "served": "", "fallbacks": "0",
                  "needs_tools": False, "note": "", "error": "", "route_ms": 0,
                  "total_ms": int((deps.now() - t0) * 1000), "chars": len(reply)})
    except Exception:
        pass
    yield {"type": "done", "routed": "gate", "served": None, "footer": foot, "entry": entry,
           "name": info["name"]}


def _new_project_turn(msgs: list, chat_id: str, entry: str, info: dict, deps: Deps, t0):
    """A + New project chat (#1151): owui_newproject's interview, PRD loop and setup. No router,
    no tools and no project context: the state machine decides each reply."""
    last = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
    text = text_of(last.get("content"))[0]
    state, error = None, ""
    if not chat_id:
        error = "a + New project chat needs a saved chat id"
    else:
        try:
            state = deps.np_load(chat_id) or owui_newproject.fresh()
        except Exception as e:
            error = f"its saved state can't be read ({type(e).__name__})"
    if error:
        yield {"type": "delta", "text": f"⚠ Can't continue this chat: {error}. Nothing was created."}
        yield {"type": "done", "routed": None, "served": None, "entry": entry, "name": info["name"]}
        return
    try:
        rows = deps.registry()
        taken = None if rows is None else {r.get("id") for r in rows}
    except Exception:
        taken = None

    def save(st):
        deps.np_save(chat_id, st)

    # The type list is read only on the turns that use it: the pitch, and until a type is chosen.
    profiles = None
    if deps.profiles and (state.get("q", -1) < 0 or state.get("type") == ""):
        try:
            profiles = deps.profiles()
        except Exception:
            profiles = []

    out = []
    try:
        for piece in owui_newproject.step(text, state, taken=taken, draft=deps.draft or _no_draft,
                                          save=save, profiles=profiles, setup=deps.setup and (
                                              lambda st, sv: deps.setup(st, sv, chat_id))):
            out.append(piece)
            yield {"type": "delta", "text": piece}
    except Exception as e:
        piece = f"\n\n⚠ {type(e).__name__}: {str(e)[:200]}. Send any message to pick up from here."
        out.append(piece)
        yield {"type": "delta", "text": piece}
    stage = state.get("stage", "")
    foot = f"new project · {stage}"
    if stage == "prd":
        foot += f" · drafted by {owui_newproject.alias(state)}"
    yield {"type": "delta", "text": f"\n\n---\n{foot}"}
    try:
        deps.log({"chat_id": chat_id, "entry": entry, "project": state.get("id") or "",
                  "local_only": owui_newproject.alias(state) == "private",
                  "routed": "new-project", "routed_by": "rule", "reason": stage, "served": "",
                  "fallbacks": "0", "needs_tools": False, "note": "", "error": "",
                  "route_ms": 0, "total_ms": int((deps.now() - t0) * 1000),
                  "chars": sum(len(p) for p in out)})
    except Exception:
        pass
    yield {"type": "done", "routed": "new-project", "served": None, "footer": foot,
           "entry": entry, "name": info["name"]}


def _delete_turn(text: str, entry: str, info: dict, chat_id: str, deps: Deps):
    """`delete project <id>` (owui_delete): a typed command, never a model's call. No router and
    no tools; the reply is the plan, the gate's state, or the steps' own results."""
    out = []
    try:
        for piece in deps.delete(text, info["project"] or "", chat_id):
            out.append(piece)
            yield {"type": "delta", "text": piece}
    except Exception as e:
        piece = f"\n\n⚠ {type(e).__name__}: {str(e)[:200]}. Send the command again to pick up from here."
        out.append(piece)
        yield {"type": "delta", "text": piece}
    foot = "delete project · no model"
    yield {"type": "delta", "text": f"\n\n---\n{foot}"}
    yield {"type": "done", "routed": "delete-project", "served": None, "footer": foot, "entry": entry,
           "name": info["name"]}


def _no_draft(alias, system, user):
    raise RuntimeError("no PRD model is wired in")


def turn(req: dict, deps: Deps):
    t0 = deps.now()
    entry, chat_id = str(req.get("entry") or ""), str(req.get("chat_id") or "")
    msgs = [m for m in (req.get("messages") or []) if isinstance(m, dict)]
    session = SESSION_PREFIX + (chat_id or "unknown")

    if req.get("task"):             # Open WebUI's own title/tags/follow-up calls: fast, local, bare
        yield from _task(msgs, deps)
        return
    entry, info, notes = resolve(entry, chat_id, deps)
    if req.get("local_only") is True and not info["local_only"]:   # NOMAD Local (#1436): tighten only
        info = {**info, "local_only": True}
    if info["kind"] == "new":
        yield from _new_project_turn(msgs, chat_id, entry, info, deps, t0)
        return
    if info["kind"] == "unknown":
        yield {"type": "delta", "text": f"⚠ No such NOMAD entry: {entry!r}. Reload the model list."}
        yield {"type": "done", "routed": None, "served": None}
        return

    last = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"), None)
    user_content = msgs[last].get("content") if last is not None else ""
    text, has_image = text_of(user_content)
    history = [{"role": m.get("role"), "content": text_of(m.get("content"))[0][:HISTORY_CHARS]}
               for m in msgs[:last if last is not None else 0]
               if m.get("role") in ("user", "assistant")][-HISTORY_TURNS:]

    if deps.gate and not has_image:
        intent, payload = intents.classify(text)
        if intent == "gate_decision":
            yield from _gate_turn(payload, text, entry, info, chat_id, session, deps, t0)
            return

    if deps.delete and not has_image and owui_delete.COMMAND.match(text):     # #1200: the command only
        yield from _delete_turn(text, entry, info, chat_id, deps)
        return
    if not has_image and DELETE_ASK.match(text):
        foot = "delete help · no model"
        yield {"type": "delta", "text": f"{DELETE_HELP}\n\n---\n{foot}"}
        yield {"type": "done", "routed": "delete-help", "served": None, "footer": foot,
               "entry": entry, "name": info["name"]}
        return

    yield {"type": "status", "text": "routing…"}
    is_project = info["kind"] == "project"
    d = owui_router.route(text, local_only=info["local_only"], complete=deps.complete,
                          history=history, has_image=has_image, context_tokens=int(is_project),
                          loaded=str(req.get("local_loaded") or ""))
    t_route = deps.now() - t0
    yield {"type": "status", "text": f"→ {d.model}"}

    tools = {}
    if not has_image and (d.needs_tools or ACTION_HINT.search(d.message)):
        try:
            tools = deps.tools(info) or {}
        except Exception as e:
            notes.append(f"tools unavailable ({type(e).__name__})")
    cloud = False
    if tools:
        d, cloud = _tool_plan(d, info, deps, notes)
    budget = (owui_router.context_budget(d.model) if not tools or cloud
              else tool_context_budget(d.model, tools))
    system = [TOOLS_SYSTEM if tools else SYSTEM]
    if is_project:
        system.append(owui_context.load(chat_id, info["project"], deps.fetch_context, deps.search,
                                        now=deps.now, budget_tokens=budget))
    else:
        system.append(GENERAL)
    try:                            # a bare `!deep` re-ask wants a fresh answer, not the last
        mems = [] if owui_router.bare(text) else (deps.recall(d.message, session) or [])
    except Exception:               # one recalled and copied (#1180)
        mems = []
    if mems:
        system.append("RECALLED MEMORY (relevant past conversations):\n"
                      + "\n".join(f"- {m['role']}: {m['content'][:200]}" for m in mems))
    asked = user_content if has_image and d.message == text else d.message
    messages = ([{"role": "system", "content": "\n\n".join(system)}] + history
                + [{"role": "user", "content": asked}])

    headers, answer, error, called = {}, [], "", []
    if tools:
        d, headers, answer, error, called = yield from _tool_turn(d, cloud, messages, tools, deps,
                                                                  notes)
        if called:
            notes.append("tools: " + ", ".join(dict.fromkeys(called)))
    else:
        try:
            headers, chunks = deps.stream(_body(d, messages))
            headers = {k.lower(): v for k, v in (headers or {}).items()}
            guard = LineGuard()         # no tools here, so the model ran nothing (#1198)
            for chunk in chunks:
                if chunk and (piece := guard.feed(chunk)):
                    answer.append(piece)
                    yield {"type": "delta", "text": piece}
            if tail := guard.flush():
                answer.append(tail)
                yield {"type": "delta", "text": tail}
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:200]}"
            msg = f"⚠ No model answered ({error})."
            answer.append(("\n\n" if answer else "") + msg)
            yield {"type": "delta", "text": ("\n\n" if len(answer) > 1 else "") + msg}
    foot = footer(d, headers, notes)
    yield {"type": "delta", "text": f"\n\n---\n{foot}"}

    reply = "".join(answer)
    served = headers.get("x-litellm-model-group")
    for role, content in (("user", d.message), ("assistant", reply)):
        try:
            deps.remember(session, role, content)
        except Exception:
            pass
    row = {"chat_id": chat_id, "entry": entry, "project": info["project"] or "",
           "local_only": info["local_only"], "routed": d.model, "routed_by": d.by,
           "reason": d.reason[:200], "served": served or "",
           "fallbacks": str(headers.get("x-litellm-attempted-fallbacks") or "0"),
           "needs_tools": d.needs_tools, "note": " · ".join(n for n in [d.note] + notes if n),
           "error": error, "route_ms": int(t_route * 1000),
           "total_ms": int((deps.now() - t0) * 1000), "chars": len(reply)}
    try:
        deps.log(row)
    except Exception:
        pass
    yield {"type": "done", "routed": d.model, "served": served, "footer": foot, "entry": entry,
           "name": info["name"]}


def context(req: dict, deps: Deps) -> dict:
    """For the global filter (§6.5): the project context to prepend for a model the operator
    picked. Bound like a turn. Nothing for General or + New project, and nothing for a
    `local_only` project when the model isn't one of the router's local models: the operator's
    pick doesn't move a private project's files to the cloud."""
    entry, chat_id = str(req.get("entry") or ""), str(req.get("chat_id") or "")
    model = str(req.get("model") or "")
    entry, info, notes = resolve(entry, chat_id, deps)
    out = {"entry": entry, "name": info["name"], "system": "", "note": " · ".join(notes)}
    if info["kind"] in ("project", "general"):
        found = _override_for(req, info)
        if found:
            out["override"] = found
            model = found["model"]
    if info["kind"] != "project":
        return out
    if info["local_only"] and model not in owui_router.LOCAL:
        out["note"] = " · ".join(n for n in (out["note"], f"{info['name']} is local-only, so its "
                                             f"context isn't sent to {model}") if n)
        return out
    budget = owui_router.context_budget(model) if model in owui_router.WINDOW else None
    out["system"] = FILTER + "\n\n" + owui_context.load(
        chat_id, info["project"], deps.fetch_context, deps.search, now=deps.now,
        budget_tokens=budget)
    return out


def _override_for(req: dict, info: dict) -> dict | None:
    """The model override in the latest user message (`/deep`, `!local`, a bare `!deep` re-ask),
    decided by owui_router.override like a NOMAD Auto turn (#1199). `messages` is the chat so far
    as [{role, content}], latest last. Returns {model, cmd, text, refused, note} or None."""
    msgs = [m for m in (req.get("messages") or []) if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")]
    if not msgs or msgs[-1].get("role") != "user":
        return None
    rows = [{"role": m["role"], "content": text_of(m.get("content"))[0]} for m in msgs]
    d = owui_router.override(rows[-1]["content"], local_only=info["local_only"], history=rows[:-1])
    if not d:
        return None
    return {"model": d.model, "cmd": d.reason.replace(" refused", ""), "text": d.message,
            "refused": bool(d.note), "note": d.note}
