"""Create a project: the interview, the PRD loop and the setup steps (#1372, docs/prd/owui-sessions.md).

One module for every entry point. The Open WebUI chat calls `step` (through v2/owui_newproject
and v2/owui_setup, which only re-export it). The engine's POST /owui/create-project, the
dispatcher's POST /create-project in front of it, and the `create_project` MCP tool for agents
call `create` (#1374), which plays the given answers through the same `step` and then runs the
same setup. It imports nothing from Open WebUI or the engine: everything it acts through comes
in as arguments (`taken`, `draft`, `save`, `setup`, `Setup`).

The interview is a small state machine per chat. The caller saves the state:

  interview  the operator's first message is the pitch; then one question per turn: the project
             type first when the caller passes `profiles` (#1373), then QUESTIONS less the
             profile's `skip`, with the profile's own questions after `stack`
  prd        the drafted PRD is shown and revised until the operator types `approve PRD`
  setup      `run` (below), which saves after each step and picks up where it stopped
  done       the project exists, and the chat is bound to it

Nothing outside the state is created before `approve PRD`: the interview and the PRD loop only
call the model. `cancel` or `start over` clears it, until setup begins. `step(text, state, …)`
yields the reply in pieces and changes `state` in place, calling `save(state)` at every stage
change.

The setup steps, in order. Each one is reported, and recorded in state["steps"] (saved) once it's
done, so a re-run skips it. A step that fails stops the run with stage still "setup": the next
message picks up at that step.

  repo      dispatcher POST /new-project: the scaffold, docs/PRD.md, git init, first commit
  registry  dispatcher POST /register-project: projects.yaml (+ autopilot block and golden
            file), nomad.code-workspace, on a hub branch merged into master
  board     dispatcher POST /seed-board: the nomad-pm board in seed.rb, seeded
  roadmap   an Initiative in nomad-portfolio, a dated Feature for M1 on the new board, its
            Epics and Tasks; each item's id is saved as it's made, so a re-run makes no twins
  stream    dispatcher POST /project-stream: Zulip #<id>
  console   the mission-control row in NocoDB `projects`
  refresh   bind the chat to the project and drop the cached project lists

The dispatcher steps run on the host, because they touch ~/nomad, ~/nomad-pm and the relay's
credentials, which the engine's container doesn't have. Nothing here creates a GitHub remote or
pushes: that is red tier, and the last message offers it as a gated request.
"""
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterator

QUESTIONS = (
    ("name", "What's the project called?"),
    ("problem", "What problem does it solve, and why now?"),
    ("users", "Who is it for?"),
    ("mvp", "What's the smallest version worth shipping (the MVP)?"),
    ("stack", "What stack: language, framework, where it runs? (Or say \"you pick\".)"),
    ("visibility", "Private repo, or public (open source)?"),
    ("local_only", "Should its chats use local models only, never a cloud model? (yes or no)"),
    ("autopilot", "Should the autopilot build its tasks? (yes or no. The switch stays off until "
                  "you turn it on in the cockpit.)"),
)
SECTIONS = ("Problem and why now", "Users", "Goals and non-goals", "Scope (MVP and later)",
            "User stories", "Requirements (functional and non-functional)", "Success metrics",
            "Risks", "Open questions", "Release plan")       # docs/SDLC.md, Large
YES = re.compile(r"^\W*(y|yes|yeah|yep|sure|ok|okay|true|on)\b", re.I)
NO = re.compile(r"^\W*(n|no|nope|false|off|not)\b", re.I)
APPROVE = re.compile(r"^\W*approve\s+(the\s+)?prd\b", re.I)
RESET = re.compile(r"^\W*(cancel|start over|restart)\W*$", re.I)
PRIVATE = re.compile(r"\b(private idea|keep (it|this) private|confidential|secret project)\b", re.I)
ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")

INTRO = ("Let's set up a new project. I'll ask {n} short questions, then draft a PRD for your OK. "
         "Nothing is created until you say `approve PRD`. Say `cancel` to stop.")
PRD_SYSTEM = (
    "You write a new project's PRD, in markdown, for the operator of NOMAD (a personal AI "
    "orchestrator). Start with `# PRD: <project name>`. Then these sections, as `##` headings, "
    "in this order: " + "; ".join(SECTIONS) + ". Use what the operator said. Where they left "
    "something open, put it under Open questions with the answer you assumed. The release plan "
    "names milestones (M1, M2…), each with what it delivers. Plain, short sentences. No preamble "
    "and no closing remarks: the reply is the file.")
TYPE_INTRO = ("Let's set up a new project. First, what kind of project it is; that decides the "
              "rest of the questions. Then I draft a PRD for your OK. Nothing is created until "
              "you say `approve PRD`. Say `cancel` to stop.")
TYPE_Q = "What kind of project is it?"
OTHER = re.compile(r"^\W*other\b\W*(.*)$", re.I | re.S)
NO_TYPES = "The list of project types can't be read right now. Send any message to try again."
UNDECIDED = re.compile(r"\bnot decided\b|\bundecided\b|\bdon'?t know\b|\bnot sure\b", re.I)
# A game's engine, once known, attaches that engine's project policy (operator, 2026-10-16).
# Other engines get an engine-<x> policy when a first project needs one.
ENGINE_POLICIES = {"unity": "engine-unity"}
NOT_WIRED = ("The setup steps (repo, registry, board, roadmap, stream) aren't wired in yet "
             "(#1153). The approved PRD is saved with this chat.")


def fresh() -> dict:
    return {"stage": "interview", "q": -1, "answers": {}, "prd": "", "id": "", "private": False}


def slug(name: str) -> str:
    """The project id for a name: dispatcher._slug's rule."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def alias(state: dict) -> str:
    """Who drafts the PRD: balanced, or private when the idea is private or local-only."""
    a = state.get("answers") or {}
    return "private" if state.get("private") or a.get("local_only") is True else "balanced"


def typed(state: dict) -> bool:
    """True when this interview asks the type (the caller passed `profiles` at the pitch)."""
    return "type" in state


def questions(state: dict) -> list:
    """The interview, as (key, question, profile question or None). Untyped: QUESTIONS. Typed:
    the type question, then QUESTIONS less the profile's skips (never `name`, which makes the
    id), with the profile's own questions, keyed `t.<id>`, after `stack`."""
    base = [(k, q, None) for k, q in QUESTIONS]
    if not typed(state):
        return base
    prof = state.get("profile")
    if not prof:
        return [("type", TYPE_Q, None)]
    skip = set(prof.get("skip") or ()) - {"name"}
    base = [b for b in base if b[0] not in skip]
    at = next((i + 1 for i, b in enumerate(base) if b[0] == "stack"),
              next((i for i, b in enumerate(base) if b[0] == "visibility"), len(base)))
    extra = [("t." + q["id"], q["question"], q) for q in prof.get("ask") or ()]
    return [("type", TYPE_Q, None)] + base[:at] + extra + base[at:]


def _options(opts) -> str:
    return "\n".join(f"{i}. {o}" for i, o in enumerate(opts, 1))


def _ask(state: dict, q: int) -> str:
    qs = questions(state)
    key, text, pq = qs[q]
    n = "" if key == "type" else f"/{len(qs)}"      # the count is known once the type is
    head = f"**{q + 1}{n}.** {text}"
    if key == "type":
        menu = state.get("menu") or []
        if not menu:
            return f"{head}\n\n{NO_TYPES}"
        rows = [f"{i}. **{m['title']}**: {m['description']}" for i, m in enumerate(menu, 1)]
        rows.append(f"{len(menu) + 1}. **Other**: say `other: <what it is>`. It is set up as "
                    "generic, and flagged for a new type.")
        return f"{head} (a number or a name)\n\n" + "\n".join(rows)
    if pq and pq.get("options") and pq.get("kind") in ("single", "multi"):
        how = "one or more numbers, or your own answer" if pq["kind"] == "multi" else \
            "a number, or your own answer"
        return f"{head} ({how})\n\n{_options(pq['options'])}"
    return head


def _shown(v):
    if isinstance(v, bool):
        return {True: "yes", False: "no"}[v]
    return ", ".join(v) if isinstance(v, list) else v


def _brief(state: dict) -> str:
    a = state["answers"]
    lines = [f"Pitch: {a.get('pitch', '')}", f"Project id: {state.get('id', '')}"]
    prof = state.get("profile") if typed(state) else None
    if prof:
        t = f"Project type: {prof['title']} ({state['type']})"
        if state.get("new_type"):
            t += (f". No type fits; the operator described it as: {state['new_type']}. It is set "
                  "up as generic; say so under Open questions (a new project type may be needed)")
        lines.append(t)
    extra = a.get("extra") or {}
    for key, question, pq in questions(state):
        if key == "type":
            continue
        v = _shown(extra.get(key[2:], "") if pq else a.get(key, ""))
        if pq and isinstance(v, str) and UNDECIDED.search(v):
            v += " (settle it in the PRD: under Open questions, with the answer you assume)"
        lines.append(f"{question}\n{v}")
    if prof and (prof.get("agents") or any((prof.get("gates") or {}).values())):
        roles = ", ".join(f"{x['role']} ({x.get('lane', 'dev')})" for x in prof.get("agents") or ())
        g = prof.get("gates") or {}
        lines.append("From the project type (name them in Requirements):"
                     + (f"\nAgent roles: {roles}" if roles else "")
                     + (f"\nPre-merge gate: {' && '.join(g['pre_merge'])}" if g.get("pre_merge") else "")
                     + (f"\nMilestone gate: {' && '.join(g['milestone'])}" if g.get("milestone") else ""))
    return "\n\n".join(lines)


def _choice(pq: dict, text: str):
    """A profile question's answer: option numbers or names become the option, anything else is
    kept as the operator wrote it. A multi question gives a list."""
    t, opts = text.strip(), list(pq.get("options") or ())
    kind = pq.get("kind") or "text"
    if kind not in ("single", "multi") or not opts:
        return t[:2000]
    if kind == "multi":
        parts = re.findall(r"\d+", t) if re.fullmatch(r"[\d\s,;]+", t) else \
            [p.strip() for p in re.split(r"[,;]|\band\b", t) if p.strip()]
    else:
        parts = [t]
    out = []
    for p in parts:
        if p.isdigit() and 1 <= int(p) <= len(opts):
            out.append(opts[int(p) - 1])
        else:
            out.append(next((o for o in opts if o.lower() == p.lower()), p[:200]))
    return list(dict.fromkeys(out)) if kind == "multi" else out[0]


def _profile(p: dict) -> dict:
    """What the state keeps of the chosen profile."""
    return {"title": p.get("title") or p["id"], "ask": list((p.get("interview") or {}).get("ask") or ()),
            "skip": list((p.get("interview") or {}).get("skip") or ()),
            "stack": dict(p.get("stack") or {}), "policies": list(p.get("policies") or ()),
            "agents": list(p.get("agents") or ()), "gates": dict(p.get("gates") or {})}


def _type_answer(state: dict, text: str, profiles) -> str:
    """Record the project type. Returns "" when taken, else why it was refused."""
    menu = state.get("menu") or list(profiles or ())
    if not menu:
        return NO_TYPES
    state["menu"] = menu
    t, pick, other = text.strip(), None, OTHER.match(text or "")
    if other:
        if not other.group(1).strip():
            return "What kind of project is it? Say `other: <what it is>`."
        pick = next((m for m in menu if m["id"] == "generic"), None)
        if pick is None:
            return "There is no generic type to fall back on. Pick one of the list."
        state["new_type"] = other.group(1).strip()[:300]
    elif t.isdigit() and 1 <= int(t) <= len(menu):
        pick = menu[int(t) - 1]
    elif t.isdigit() and int(t) == len(menu) + 1:
        return "Say `other: <what it is>`, so I can note what kind of project it is."
    else:
        pick = next((m for m in menu if t.lower() in (m["id"], m["title"].lower())), None)
        if pick is None:
            return "Pick a number from the list, or say `other: <what it is>`."
    state["type"], state["profile"] = pick["id"], _profile(pick)
    state.pop("menu", None)
    return ""


def project_policies(state: dict) -> list:
    """Policies attached to this project alone: the game engine's, once it is known."""
    if state.get("type") != "game":
        return []
    eng = ((state.get("answers") or {}).get("extra") or {}).get("engine")
    pol = ENGINE_POLICIES.get(str(eng or "").strip().lower())
    return [pol] if pol else []


def clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, re.S)
    return (m.group(1) if m else text).strip()


def _answer(state: dict, key: str, text: str, taken, pq=None) -> str:
    """Record the answer to `key`. Returns "" when taken, else why it was refused."""
    a, t = state["answers"], text.strip()
    if pq is not None:
        if not t:
            return "Please answer, or say `you pick`."
        a.setdefault("extra", {})[pq["id"]] = _choice(pq, t)
    elif key == "name":
        pid = slug(t)
        if not ID.match(pid):
            return "That name doesn't make a usable id (letters, digits and dashes, 2–40). Another name?"
        if taken is None:
            return "The project registry can't be read right now, so I can't check the name. Try again in a minute."
        if pid in taken:
            return f"`{pid}` is already a project. Another name?"
        a["name"], state["id"] = t[:80], pid
    elif key in ("local_only", "autopilot"):
        if YES.match(t):
            a[key] = True
        elif NO.match(t):
            a[key] = False
        else:
            return "Please answer yes or no."
    elif key == "visibility":
        a[key] = "public" if re.search(r"\b(public|open[ -]?source)\b", t, re.I) else "private"
    else:
        a[key] = t[:2000]
    if PRIVATE.search(t):
        state["private"] = True
    return ""


def _draft(state: dict, draft: Callable, feedback: str = "") -> Iterator[str]:
    user = _brief(state)
    if feedback and state.get("prd"):
        user = (f"{user}\n\nThe current PRD:\n\n{state['prd']}\n\nThe operator's changes:\n"
                f"{feedback}\n\nReturn the whole revised PRD.")
    try:
        prd = clean(draft(alias(state), PRD_SYSTEM, user))
    except Exception as e:
        yield (f"⚠ Drafting the PRD failed ({type(e).__name__}: {str(e)[:150]}). Send any "
               "message to try again.")
        return
    if not prd:
        yield "⚠ The model returned an empty PRD. Send any message to try again."
        return
    state["prd"] = prd
    yield (f"{prd}\n\n---\nSay what to change, or `approve PRD` to set the project up. Nothing "
           "is created before that.")


def step(text: str, state: dict, *, taken, draft: Callable, save: Callable,
         setup: Callable | None = None, profiles=None) -> Iterator[str]:
    """One operator message. `taken`: the registry's project ids, or None when it can't be
    read. `draft(alias, system, user)` returns the model's text. `setup(state, save)` yields
    progress lines and sets state["stage"] = "done" when it finishes. `profiles`: the project
    types, as the dispatcher's GET /profiles lists them; None asks no type (the interview as it
    was before #1373), an empty list means they can't be read right now."""
    stage = state.get("stage") or "interview"
    if RESET.match(text or "") and stage in ("interview", "prd"):
        state.clear()
        state.update(fresh())
        save(state)
        yield "Cleared. Tell me about the new project when you're ready."
        return

    if stage == "interview":
        q = state.get("q", -1)
        if q < 0:
            state["answers"]["pitch"] = (text or "").strip()[:2000]
            state["private"] = bool(PRIVATE.search(text or ""))
            state["q"] = 0
            if profiles is not None:
                state["type"], state["menu"] = "", list(profiles)
            save(state)
            intro = TYPE_INTRO if typed(state) else INTRO.format(n=len(QUESTIONS))
            yield f"{intro}\n\n{_ask(state, 0)}"
            return
        key, _, pq = questions(state)[q]
        why = (_type_answer(state, text or "", profiles) if key == "type"
               else _answer(state, key, text or "", taken, pq))
        if why:
            save(state)
            yield f"{why}\n\n{_ask(state, q)}"
            return
        state["q"] = q + 1
        if q + 1 < len(questions(state)):
            save(state)
            yield _ask(state, q + 1)
            return
        state["stage"] = "prd"
        save(state)
        yield from _draft(state, draft)
        save(state)
        return

    if stage == "prd":
        if not state.get("prd"):
            yield from _draft(state, draft)
        elif APPROVE.match(text or ""):
            state["stage"] = "setup"
            save(state)
            yield f"PRD approved. Setting up **{state['answers'].get('name')}** (`{state['id']}`).\n\n"
            yield from setup(state, save) if setup else iter([NOT_WIRED])
        else:
            yield from _draft(state, draft, feedback=text or "")
        save(state)
        return

    if stage == "setup":
        yield from setup(state, save) if setup else iter([NOT_WIRED])
        return

    yield (f"This chat set up **{state['answers'].get('name')}**. Its chats are under "
           f"`{state['id']}` in the Project dropdown.")


# ── create: the same flow from answers given all at once (#1374) ───────────────────────

# What `create` takes. Only name and prd are required; a question left out gets the answer an
# operator gives when they leave it open, so the result is what the chat makes for the same words.
INPUTS = {
    "name": "the project name; its slug is the id",
    "prd": "the PRD the operator approved, in markdown (the chat's `approve PRD`)",
    "pitch": "the first message: what the project is (default: problem, then name)",
    "type": "a project type id from GET /profiles (default generic)",
    "new_type": "no type fits: what it is (set up as generic, flagged for a new type)",
    "problem": "", "users": "", "mvp": "",
    "stack": "words or a list; empty means \"you pick\" (the type's stack defaults)",
    "visibility": "private (default) or public",
    "local_only": "true: its chats use local models only",
    "autopilot": "true: an autopilot block (its switch stays off)",
    "extra": "the type's own questions, by id: an option, a list for multi, or your words",
}


def _said(v, default: str = "") -> str:
    """An input as the words an operator would type."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return default if v is None or str(v).strip() == "" else str(v)


def _reply_for(key: str, inputs: dict) -> str:
    if key == "type":
        return f"other: {inputs['new_type']}" if _said(inputs.get("new_type")) else \
            _said(inputs.get("type"), "generic")
    if key.startswith("t."):
        return _said((inputs.get("extra") or {}).get(key[2:]), "you pick")
    if key in ("local_only", "autopilot"):
        v = inputs.get(key)
        return v if isinstance(v, str) and v.strip() else _said(v is True)
    return _said(inputs.get(key), {"stack": "you pick", "visibility": "private"}.get(key, ""))


def prepare(inputs: dict, *, taken, profiles=None) -> tuple:
    """(state, "") with the interview done and the PRD approved, as the chat leaves it at
    `approve PRD` for the same answers; or (None, why) when an answer is refused. The answers go
    through `step` one by one, so every check the chat makes is made here too."""
    inputs = inputs if isinstance(inputs, dict) else {}
    prd = clean(_said(inputs.get("prd")))
    if not _said(inputs.get("name")).strip():
        return None, "A project needs a name."
    if not prd:
        return None, "A project needs the PRD the operator approved (`prd`)."
    st = fresh()
    pitch = _said(inputs.get("pitch")) or _said(inputs.get("problem")) or _said(inputs.get("name"))

    def say(text):
        return "".join(step(text, st, taken=taken, draft=lambda *a: prd, save=lambda s: None,
                            profiles=profiles))

    say(pitch)
    while st["stage"] == "interview":
        at = st["q"]
        key = questions(st)[at][0]
        out = say(_reply_for(key, inputs))
        if st["stage"] == "interview" and st["q"] == at:
            return None, f"{key}: {out.split(chr(10) * 2)[0]}"
    if st.get("prd") != prd:
        return None, "The PRD could not be taken."
    st["stage"] = "setup"
    return st, ""


def create(inputs: dict, state: dict, *, taken, save: Callable, setup: Callable,
           profiles=None) -> Iterator[str]:
    """Set a project up from answers given at once (INPUTS). `state` is this run's saved state,
    {} for a new one: a run that stopped at a step picks up there, as the chat does, and a done
    run is only reported. `setup(state, save)` is the chat's (run with a Setup)."""
    if state.get("stage") not in ("setup", "done"):
        st, why = prepare(inputs, taken=taken, profiles=profiles)
        if why:
            yield f"⚠ Not created: {why}"
            return
        state.clear()
        state.update(st)
        save(state)
        yield (f"Setting up **{state['answers']['name']}** (`{state['id']}`)"
               + (f" as type `{state['type']}`" if typed(state) else "") + ".\n\n")
    elif slug(_said(inputs.get("name"))) not in ("", state.get("id")):
        yield f"⚠ This run is for `{state.get('id')}`, not `{slug(_said(inputs.get('name')))}`."
        return
    if state["stage"] == "done":
        yield f"**{state['answers']['name']}** (`{state['id']}`) is already set up."
        return
    yield from setup(state, save)


# ── setup: the steps after `approve PRD` (#1153) ────────────────────────────────────────

PORTFOLIO = "nomad-portfolio"
M1_DAYS = 14                        # the Feature's default span when the plan names none
PLAN_SYSTEM = (
    "You turn a project's PRD into the first milestone's work plan. Reply with JSON only, no "
    "prose and no code fence: {\"m1\": \"what M1 delivers, one line\", \"weeks\": <1-8>, "
    "\"epics\": [{\"title\": \"...\", \"tasks\": [\"...\", ...]}, ...]}. 2 to 4 epics, 2 to 5 "
    "tasks each. Each task is one sitting of work, named as an action (\"Add the tide API "
    "client\"). The first epic's first task is \"Set the test command in projects.yaml\".")
STEPS = ("repo", "registry", "board", "roadmap", "stream", "console", "refresh")
TITLES = {"repo": "Repo", "registry": "Registry", "board": "nomad-pm board",
          "roadmap": "Roadmap", "stream": "Zulip stream", "console": "Mission control",
          "refresh": "Project dropdown"}


@dataclass
class Setup:
    """What the steps act through. All of them may raise; a raise fails that step only."""
    dispatch: Callable              # (route, body, timeout) -> dict, the dispatcher's JSON
    op: Callable                    # (board ident) -> pm_openproject.OpenProject
    plan: Callable                  # (alias, system, user) -> the model's text
    upsert_project: Callable        # (row) -> None: NocoDB `projects`, keyed by title
    refresh: Callable               # (chat_id, project id) -> "" or a note on what's missing
    today: Callable = date.today


class StepFailed(Exception):
    pass


def stack_of(text: str) -> list:
    """The interview's stack answer as Nomad.md's list: short comma-separated words, or none."""
    if not text or re.search(r"\byou pick\b|\bany\b|\bdunno\b|not sure", text, re.I):
        return []
    parts = [re.sub(r"[^a-z0-9.+#-]+", "-", p.strip().lower()).strip("-")
             for p in re.split(r",|/|\band\b|\+", text)]
    return [p for p in parts if 0 < len(p) <= 24][:6]


def stack_for(state: dict) -> list:
    """The stack answer, or the type's stack defaults when the operator left it to us."""
    got = stack_of(state["answers"].get("stack", ""))
    if got or not typed(state):
        return got
    st = (state.get("profile") or {}).get("stack") or {}
    return [str(v).strip().lower() for v in st.values() if isinstance(v, str) and v.strip()][:6]


def prd_path(state: dict) -> str:
    return f"{state.get('path') or '~/' + state['id']}/docs/PRD.md"


def _ok(res: dict, what: str) -> dict:
    if not isinstance(res, dict) or not res.get("ok"):
        raise StepFailed(f"{what}: {(res or {}).get('error') or 'no answer'}")
    return res


# ── the steps: each returns the line reported in the chat ───────────────────────────────

def _repo(s: Setup, state: dict, save) -> str:
    a = state["answers"]
    res = _ok(s.dispatch("/new-project", {
        "name": a["name"], "id": state["id"], "description": a.get("problem", ""),
        "prd": state["prd"], "stack": stack_for(state), "lane": "dev",
        "local_only": a.get("local_only") is True, "register": False}, 120), "/new-project")
    state["path"] = res.get("path") or ""
    return f"`{state['path'] or state['id']}`, first commit made, PRD in docs/PRD.md"


def _registry(s: Setup, state: dict, save) -> str:
    a = state["answers"]
    body = {"id": state["id"], "local_only": a.get("local_only") is True,
            "autopilot": a.get("autopilot") is True}
    if typed(state):        # the dispatcher reads the type's gates from its profile itself
        body.update({"type": state["type"], "policies": project_policies(state)})
    res = _ok(s.dispatch("/register-project", body, 660), "/register-project")
    line = "added to projects.yaml" + (f" as type `{state['type']}`" if typed(state) else "") + (
        f" ({', '.join(body['policies'])} attached)" if body.get("policies") else "") + (
        (", with" if typed(state) else " with") + " an autopilot block (switch off)"
        if a.get("autopilot") else "")
    return f"{line}. ⚠ {res['note']}" if res.get("note") and not res.get("merged") else line


def _board(s: Setup, state: dict, save) -> str:
    res = _ok(s.dispatch("/seed-board", {"board": state["id"], "name": state["answers"]["name"]},
                         960), "/seed-board")
    if res.get("op_id"):
        state["op_id"] = res["op_id"]
    line = f"board `{state['id']}` seeded"
    return f"{line}. ⚠ {res['note']}" if res.get("note") and not res.get("merged") else line


def parse_plan(text: str, state: dict) -> dict:
    """The model's plan, checked; a plain fallback when it isn't usable."""
    try:
        raw = clean(text)
        raw = raw[raw.index("{"):raw.rindex("}") + 1]
        p = json.loads(raw)
        epics = [{"title": str(e["title"]).strip()[:200],
                  "tasks": [str(t).strip()[:200] for t in e.get("tasks") or [] if str(t).strip()][:6]}
                 for e in p.get("epics") or [] if str(e.get("title", "")).strip()][:5]
        if epics and all(e["tasks"] for e in epics):
            weeks = p.get("weeks")
            weeks = int(weeks) if isinstance(weeks, (int, float)) and 1 <= weeks <= 8 else 2
            return {"m1": str(p.get("m1") or "").strip()[:200] or "First milestone",
                    "weeks": weeks, "epics": epics}
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    mvp = (state["answers"].get("mvp") or "the MVP").strip()
    return {"m1": mvp[:200], "weeks": 2, "epics": [{"title": "MVP", "tasks": [
        "Set the test command in projects.yaml", f"Build the MVP: {mvp[:150]}",
        "Write how to run it in the README"]}], "fallback": True}


def _roadmap(s: Setup, state: dict, save) -> str:
    made = state.setdefault("roadmap", {})          # ref -> work package id
    if "plan" not in made:
        try:
            text = s.plan(alias(state), PLAN_SYSTEM, state["prd"])
        except Exception:
            text = ""
        made["plan"] = parse_plan(text, state)
        save(state)
    plan, name, ref = made["plan"], state["answers"]["name"], prd_path(state)
    desc = f"From the approved PRD: `{ref}`."

    def once(key, board, type_name, subject, parent=None):
        if key not in made:
            wp = s.op(board).create(type_name, subject, parent=parent, lane="manager" if
                                    type_name in ("Initiative", "Feature") else "dev",
                                    description=desc)
            made[key] = wp["id"]
            save(state)
        return made[key]

    init = once("initiative", PORTFOLIO, "Initiative", name)
    feat = once("feature", state["id"], "Feature", f"M1: {plan['m1']}", init)
    if "dates" not in made:
        op = s.op(state["id"])
        wp = op.get(feat)
        start = s.today()
        op.call("PATCH", f"/work_packages/{feat}", {
            "lockVersion": wp["lockVersion"], "startDate": start.isoformat(),
            "dueDate": (start + timedelta(weeks=plan["weeks"])).isoformat()})
        made["dates"] = True
        save(state)
    tasks = 0
    for i, epic in enumerate(plan["epics"]):
        eid = once(f"epic{i}", state["id"], "Epic", epic["title"], feat)
        for j, task in enumerate(epic["tasks"]):
            once(f"task{i}.{j}", state["id"], "Task", task, eid)
            tasks += 1
    line = (f"Initiative #{init} in {PORTFOLIO}, Feature #{feat} (M1, {plan['weeks']} "
            f"week{'s' if plan['weeks'] != 1 else ''}), "
            f"{len(plan['epics'])} epics, {tasks} tasks")
    return line + (" (a starter plan: the model's plan wasn't usable)" if plan.get("fallback") else "")


def _stream(s: Setup, state: dict, save) -> str:
    res = _ok(s.dispatch("/project-stream", {"id": state["id"]}, 90), "/project-stream")
    return f"#{res.get('stream') or state['id']}, with you and the bot subscribed"


def _console(s: Setup, state: dict, save) -> str:
    a = state["answers"]
    s.upsert_project({"title": a["name"], "status": "Planning", "owner": "Josias", "lane": "dev",
                      "stack": ", ".join(stack_for(state)), "repo": ""})
    return "row added to the projects table"


def _refresh(s: Setup, state: dict, save, chat_id: str = "") -> str:
    note = s.refresh(chat_id, state["id"])
    line = f"`{state['id']}` is in the Project dropdown, and this chat now works on it"
    return f"{line}. ⚠ {note}" if note else line


RUN = {"repo": _repo, "registry": _registry, "board": _board, "roadmap": _roadmap,
       "stream": _stream, "console": _console}


def _type_notes(state: dict) -> Iterator[str]:
    """What the type leaves for later: no fitting type, or a game engine not chosen yet."""
    if not typed(state):
        return
    if state.get("new_type"):
        yield (f"\nNo project type fitted ({state['new_type']}), so it is set up as generic. A "
               "new type can be added under Standards in the cockpit.\n")
    eng = ((state["answers"].get("extra") or {}).get("engine") or "")
    if state.get("type") == "game" and not project_policies(state):
        yield (f"\nThe engine ({eng or 'not given'}) has no engine policy attached. Once the PRD "
               "settles on Unity, add `policies: [engine-unity]` to its projects.yaml entry.\n")


def run(s: Setup, state: dict, save: Callable, chat_id: str = "") -> Iterator[str]:
    """Run the steps not done yet, yielding one line each. Sets state["stage"] = "done" at the end."""
    done = state.setdefault("steps", [])
    for name in STEPS:
        n = STEPS.index(name) + 1
        if name in done:
            continue
        try:
            line = _refresh(s, state, save, chat_id) if name == "refresh" else RUN[name](s, state, save)
        except Exception as e:
            why = str(e) if isinstance(e, StepFailed) else f"{type(e).__name__}: {str(e)[:200]}"
            save(state)
            yield (f"⚠ {n}/{len(STEPS)} {TITLES[name]} failed: {why}\n\nThe steps before it are "
                   "kept. Send any message to retry from this step.")
            return
        done.append(name)
        save(state)
        yield f"✓ {n}/{len(STEPS)} {TITLES[name]}: {line}\n"
    state["stage"] = "done"
    save(state)
    yield from _type_notes(state)
    yield (f"\n**{state['answers']['name']}** is set up. Keep going here, or pick "
           f"`{state['id']}` in the Project dropdown for a new chat on it. The autopilot switch is off; turn it on in the cockpit "
           "when you want it. There's no GitHub remote yet: ask for one here and it goes to the "
           "gate like any push.")
