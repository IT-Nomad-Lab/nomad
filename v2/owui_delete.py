"""Delete a project from an Open WebUI chat (#1200, docs/prd/owui-sessions.md §6.6): the reverse of
owui_setup. Deleting data is red tier, so it is gated and every step is real.

Only the operator's command starts it: `delete project <id>` (the id may be left out in a chat that
is bound to the project). A model never decides this, and nothing here is a tool.

  1. the command: the project is checked (refusals below) and a plan is built from what exists
     now. The reply lists exactly what will go and files a run at the engine's human gate
     (action `delete_project`). NOTHING has run.
  2. the gate: `approve` typed in this chat (owui_chat's gate path), or approve in the cockpit.
     Approving that run changes nothing by itself; it is only the operator's yes.
  3. the same command again: once the run is approved, the steps run and are reported one by one.
     A step that fails stops the run, stage stays "running", and the same command again picks up
     at that step. Each step is recorded in state["steps"] as it finishes, so none runs twice.

  repo      dispatcher POST /archive-project: the repo moves to ~/archive/<id> (never erased)
  registry  dispatcher POST /unregister-project: projects.yaml entry, golden file and
            nomad.code-workspace, on a hub branch merged into master
  board     dispatcher POST /archive-board (seed.rb row removed, OpenProject board archived), then
            the project's Initiative in nomad-portfolio moves to Won't do
  stream    dispatcher POST /archive-stream: the Zulip stream is archived
  console   NocoDB rows: `projects`, `owui_new_projects`, `owui_chats` bindings
  refresh   the dropdown's caches, and this command's own saved state

Refused: hub repos, ids that are not in projects.yaml, projects that share a board, and projects
with a git remote unless the same command says `confirm remote`. The remote is never touched.
"""
import re
from dataclasses import dataclass
from typing import Callable, Iterator

PORTFOLIO = "nomad-portfolio"
PROJECT_PREFIX = "p-"                   # owui.PROJECT_PREFIX; the binding's entry is p-<id>
STATE_PREFIX = "del:"                   # the chat's saved state is owui_new_projects[del:<chat id>]
ACTION = "delete_project"
STEPS = ("repo", "registry", "board", "stream", "console", "refresh")
TITLES = {"repo": "Repo", "registry": "Registry", "board": "nomad-pm board", "stream": "Zulip stream",
          "console": "Mission control", "refresh": "Project dropdown"}
COMMAND = re.compile(r"^\W*delete\s+project\b(?P<rest>.*)$", re.I | re.S)
CONFIRM = re.compile(r"\b(confirm(ed)?[- ]remote|remote[- ]confirmed)\b", re.I)
ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
SOON = ("executing",)


@dataclass
class Delete:
    """What the steps act through. All of them may raise; a raise fails that step only."""
    dispatch: Callable              # (route, body, timeout) -> dict, the dispatcher's JSON
    op: Callable                    # (board ident) -> pm_openproject.OpenProject
    rows: Callable                  # (table, field, value) -> [row]; raises when it can't be read
    drop_row: Callable              # (table, row id) -> None
    initiative: Callable            # (project id, name) -> the Initiative's work package id, or None
    file_gate: Callable             # (project id, text) -> run id, filed awaiting approval
    gate_status: Callable           # (run id) -> the run's status, or None
    refresh: Callable               # (chat_id, project id) -> "" or a note on what's missing
    drop_state: Callable            # (project id) -> None: this command's saved state, last of all


class StepFailed(Exception):
    pass


def parse(text: str):
    """(project id or "", confirmed remote) for a `delete project …` message, else None."""
    m = COMMAND.match(text or "")
    if not m:
        return None
    rest = m.group("rest")
    confirm = bool(CONFIRM.search(rest))
    rest = CONFIRM.sub(" ", rest)
    for word in rest.replace("`", " ").replace('"', " ").split():
        word = word.strip(".,:;!?'").lower()
        if word:
            return word, confirm
    return "", confirm


def _ok(res: dict, what: str) -> dict:
    if not isinstance(res, dict) or not res.get("ok"):
        raise StepFailed(f"{what}: {(res or {}).get('error') or (res or {}).get('result') or 'no answer'}")
    return res


def _count(n: int, what: str) -> str:
    return f"{n} {what}{'' if n == 1 else 's'}"


# ── the plan ─────────────────────────────────────────────────────────────────────────────

def build_plan(pid: str, confirm_remote: bool, d: Delete) -> tuple:
    """(plan, "") from what exists now, or (None, why the project can't be deleted)."""
    if not ID.match(pid or ""):
        return None, f"`{pid}` is not a project id. Say `delete project <id>`."
    try:
        info = d.dispatch("/project-info", {"id": pid}, 30)
    except Exception as e:
        info = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}
    if not isinstance(info, dict) or not info.get("ok"):
        return None, f"I can't check `{pid}` ({(info or {}).get('error') or 'no answer'}), so nothing was changed."
    if not info.get("known") or info.get("refuse"):
        return None, info.get("refuse") or f"`{pid}` is not a project."
    remote = list(info.get("remote") or [])
    if remote and not confirm_remote:
        return None, (f"`{pid}` has a git remote ({', '.join(remote)}). Deleting here never touches "
                      f"the remote, but the local repo would move to the archive. If that is what "
                      f"you want, say `delete project {pid} confirm remote`.")
    name = info.get("name") or pid
    try:
        mine = [r for r in d.rows("owui_new_projects", "project", pid)
                if not str(r.get("chat_id") or "").startswith(STATE_PREFIX)]
        console = d.rows("projects", "title", name)
        bound = d.rows("owui_chats", "entry", PROJECT_PREFIX + pid)
        init = d.initiative(pid, name)
    except Exception as e:
        return None, f"I can't read what belongs to `{pid}` ({type(e).__name__}: {str(e)[:150]}), so nothing was changed."
    return {"id": pid, "name": name, "path": info.get("path"), "archive": info.get("archive"),
            "board": info.get("board") or pid, "initiative": init, "stream": pid, "remote": remote,
            "confirm_remote": bool(confirm_remote), "golden": bool(info.get("golden")),
            "dirty": int(info.get("dirty") or 0),
            "rows": {"projects": len(console), "owui_new_projects": len(mine), "owui_chats": len(bound)}}, ""


def describe(plan: dict) -> str:
    r = plan["rows"]
    init = (f"close Initiative #{plan['initiative']} in {PORTFOLIO} as Won't do" if plan["initiative"]
            else f"no Initiative found in {PORTFOLIO} for it, so none to close")
    lines = [
        f"1. **Repo:** move `{plan['path']}` to `{plan['archive']}` (kept, not erased)"
        + (f"; {_count(plan['dirty'], 'uncommitted change')} go with it" if plan["dirty"] else ""),
        "2. **Registry:** remove its entry from `projects.yaml`"
        + (" and its golden settings file" if plan["golden"] else "")
        + ", regenerate `nomad.code-workspace`, on a hub branch merged into master",
        f"3. **Board:** archive the nomad-pm board `{plan['board']}` (its work packages stay, "
        f"archived) and {init}",
        f"4. **Zulip:** archive the stream #{plan['stream']}",
        f"5. **NocoDB:** remove {_count(r['projects'], 'row')} in `projects`, "
        f"{_count(r['owui_new_projects'], 'row')} in `owui_new_projects`, "
        f"{_count(r['owui_chats'], 'chat binding')} in `owui_chats`",
        "6. **Dropdown:** refresh the Project dropdown"]
    kept = ("The git remote " + ", ".join(plan["remote"]) + " is never touched. ") if plan["remote"] else ""
    return "\n".join(lines) + f"\n\n{kept}Nothing has run."


def _ask(plan: dict, run_id: str) -> str:
    return (f"**Delete project `{plan['id']}`?** This is what will go:\n\n{describe(plan)}\n\n"
            f"It waits at the gate as run `{run_id}`. Say `approve` here (it shows you what you "
            f"approve, then say `approve` again), or approve it in the cockpit; `reject` cancels. "
            f"After that, send `delete project {plan['id']}` and I run the steps.")


# ── the steps: each returns the line reported in the chat ───────────────────────────────

def _repo(d: Delete, state: dict, chat_id: str) -> str:
    plan = state["plan"]
    res = _ok(d.dispatch("/archive-project", {"id": plan["id"], "confirm_remote": plan["confirm_remote"]},
                         120), "/archive-project")
    line = (f"already in `{res.get('path')}`" if res.get("existed")
            else f"`{plan['path']}` moved to `{res.get('path')}`")
    return f"{line}. ⚠ {res['note']}" if res.get("note") and not res.get("existed") else line


def _registry(d: Delete, state: dict, chat_id: str) -> str:
    res = _ok(d.dispatch("/unregister-project", {"id": state["id"]}, 660), "/unregister-project")
    if res.get("note") == "already unregistered":
        return "was already out of projects.yaml"
    line = ("removed from projects.yaml" + (", golden file removed" if res.get("golden") else "")
            + ", nomad.code-workspace regenerated")
    return f"{line}. ⚠ {res['note']}" if res.get("note") and not res.get("merged") else line


def _board(d: Delete, state: dict, chat_id: str) -> str:
    plan, done = state["plan"], state.setdefault("board", {})
    notes = []
    if "archive" not in done:
        res = _ok(d.dispatch("/archive-board", {"board": plan["board"]}, 960), "/archive-board")
        done["archive"] = "archived" if res.get("archived") else "none"
        if res.get("note") and not res.get("merged", True):
            done["note"] = res["note"]
    bits = [f"board `{plan['board']}` archived" if done["archive"] == "archived"
            else f"board `{plan['board']}` was not in OpenProject, nothing to archive"]
    if done.get("note"):
        notes.append(done["note"])
    if plan["initiative"] and "initiative" not in done:
        d.op(PORTFOLIO).move(plan["initiative"], "Won't do",
                             f"Project `{plan['id']}` was deleted from Open WebUI (#1200). Its repo is "
                             f"in the archive and its board is archived.")
        done["initiative"] = plan["initiative"]
    bits.append(f"Initiative #{plan['initiative']} moved to Won't do" if plan["initiative"]
                else f"no Initiative in {PORTFOLIO} to close")
    return ", ".join(bits) + "".join(f". ⚠ {n}" for n in notes)


def _stream(d: Delete, state: dict, chat_id: str) -> str:
    res = _ok(d.dispatch("/archive-stream", {"id": state["id"]}, 90), "/archive-stream")
    return (f"#{res.get('stream') or state['id']} was not in Zulip, nothing to archive"
            if "no such" in str(res.get("result")) else f"#{res.get('stream') or state['id']} archived")


def _console(d: Delete, state: dict, chat_id: str) -> str:
    plan, gone = state["plan"], {}
    for table, field, value in (("projects", "title", plan["name"]),
                                ("owui_new_projects", "project", plan["id"]),
                                ("owui_chats", "entry", PROJECT_PREFIX + plan["id"])):
        n = 0
        for row in d.rows(table, field, value):
            if str(row.get("chat_id") or "").startswith(STATE_PREFIX):
                continue                    # this command's own state goes last, in `refresh`
            d.drop_row(table, row["Id"])
            n += 1
        gone[table] = n
    return (f"removed {_count(gone['projects'], 'row')} from projects, "
            f"{_count(gone['owui_new_projects'], 'row')} from owui_new_projects, "
            f"{_count(gone['owui_chats'], 'chat binding')}")


def _refresh(d: Delete, state: dict, chat_id: str) -> str:
    note = d.refresh(chat_id, state["id"])
    d.drop_state(state["id"])
    line = f"`{state['id']}` is out of the Project dropdown"
    return f"{line}. ⚠ {note}" if note else line


RUN = {"repo": _repo, "registry": _registry, "board": _board, "stream": _stream,
       "console": _console, "refresh": _refresh}


def run(d: Delete, state: dict, save: Callable, chat_id: str = "") -> Iterator[str]:
    """Run the steps not done yet, yielding one line each. Sets state["stage"] = "done" at the end."""
    done = state.setdefault("steps", [])
    for name in STEPS:
        n = STEPS.index(name) + 1
        if name in done:
            continue
        try:
            line = RUN[name](d, state, chat_id)
        except Exception as e:
            why = str(e) if isinstance(e, StepFailed) else f"{type(e).__name__}: {str(e)[:200]}"
            save(state)
            yield (f"⚠ {n}/{len(STEPS)} {TITLES[name]} failed: {why}\n\nThe steps before it are kept "
                   f"and nothing after it ran. Send `delete project {state['id']}` again to retry "
                   "from this step.")
            return
        done.append(name)
        state["stage"] = "done"
        if name != STEPS[-1]:
            state["stage"] = "running"
            save(state)             # the last step has just erased the saved state: keep it erased
        yield f"✓ {n}/{len(STEPS)} {TITLES[name]}: {line}\n"
    plan = state["plan"]
    kept = f" The git remote ({', '.join(plan['remote'])}) was not touched." if plan["remote"] else ""
    yield (f"\n**{plan['name']}** (`{plan['id']}`) is deleted. The repo is in `{plan['archive']}`.{kept} "
           "This chat has no project now: pick another one in the Project dropdown.")


# ── the chat turn ───────────────────────────────────────────────────────────────────────

def turn(text: str, bound: str, chat_id: str, d: Delete, load: Callable, save: Callable) -> Iterator[str]:
    """One `delete project …` message. `bound` is the project this chat is bound to, or "".
    `load()` / `save(state)` keep this chat's state. Yields the reply in pieces."""
    pid, confirm = parse(text) or ("", False)
    pid = pid or bound
    if not pid:
        yield "Which project? Say `delete project <id>`. Nothing was changed."
        return
    try:
        state = load()
    except Exception as e:
        yield f"⚠ Can't read this chat's saved state ({type(e).__name__}). Nothing was changed."
        return
    live = state if state and state.get("kind") == "delete" and state.get("stage") in ("gate", "running") else None
    if live and live["id"] != pid:
        yield (f"A delete of `{live['id']}` is {'waiting for approval' if live['stage'] == 'gate' else 'part-way'} "
               f"in this chat. Finish or reject that first: `delete project {live['id']}`.")
        return
    if live and live["stage"] == "gate" and confirm and not live["plan"]["confirm_remote"]:
        live = None                                     # a different plan: ask again
    if not live:
        plan, why = build_plan(pid, confirm, d)
        if not plan:
            yield f"{why}\n\nNothing was changed."
            return
        try:
            run_id = d.file_gate(pid, describe(plan))
        except Exception as e:
            yield (f"⚠ I couldn't file the approval request ({type(e).__name__}: {str(e)[:150]}). "
                   "Nothing was changed.")
            return
        state = {"kind": "delete", "id": pid, "stage": "gate", "run_id": run_id, "plan": plan, "steps": []}
        save(state)
        yield _ask(plan, run_id)
        return
    if live["stage"] == "gate":
        try:
            status = d.gate_status(live["run_id"])
        except Exception as e:
            yield f"⚠ I can't read the approval ({type(e).__name__}). Nothing was changed."
            return
        if status == "awaiting-approval" or status in SOON:
            yield (f"Still waiting for your approval (run `{live['run_id']}`). Say `approve` here or "
                   "approve it in the cockpit; `reject` cancels. Nothing has run.")
            return
        if status != "executed":
            live["stage"] = "cancelled"
            save(live)
            yield (f"Run `{live['run_id']}` was not approved (it is {status or 'gone'}). Nothing was "
                   f"deleted. Say `delete project {pid}` to ask again.")
            return
        live["stage"] = "running"
        save(live)
        yield f"Approved. Deleting **{live['plan']['name']}** (`{pid}`).\n\n"
    yield from run(d, live, save, chat_id)
