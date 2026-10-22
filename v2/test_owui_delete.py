"""#1200 · delete a project from an Open WebUI chat (owui_delete.py, docs/prd/owui-sessions.md §6.6).

What they pin down:
  - the command alone files a gate run and lists what will go; NOTHING runs before approval
  - refusals (hub, unknown, shared board, a git remote without `confirm remote`) file no gate
  - after approval the six steps run in order, each line comes from that step's real result
  - a failed step stops the run; the same command resumes there and nothing runs twice
  - a rejected or pending gate runs nothing; the remote is never touched
  - the chat path: only the typed command reaches it, with no model; a gate run is filed through
    the engine's file_gate and its approval is the engine's own resume_run

Run: python3 -m pytest v2/test_owui_delete.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import owui_chat as O  # noqa: E402
import owui_delete as D  # noqa: E402

INFO = {"ok": True, "known": True, "hub": False, "path": "/home/op/tide-log", "name": "Tide Log",
        "board": "tide-log", "remote": [], "dirty": 0, "golden": True,
        "archive": "/home/op/archive/tide-log", "refuse": ""}


class FakeOP:
    def __init__(self, log):
        self.log = log

    def __call__(self, board):
        self.board = board
        return self

    def move(self, wp, status, comment=""):
        self.log.append(("move", self.board, wp, status, comment))


class World:
    """Every outside thing the steps touch, recording each call. `fail` names a route that fails
    once; `info` is what /project-info says."""

    def __init__(self, info=None, fail=(), status="awaiting-approval", initiative=1183):
        self.calls, self.gates, self.dropped, self.refreshed, self.state_dropped = [], [], [], [], []
        self.info, self.fail, self.status, self.init = dict(info or INFO), set(fail), status, initiative
        self.tables = {"projects": [{"Id": 4, "title": "Tide Log"}],
                       "owui_new_projects": [{"Id": 7, "chat_id": "np1", "project": "tide-log"},
                                             {"Id": 8, "chat_id": "del:c1", "project": "tide-log"}],
                       "owui_chats": [{"Id": 2, "chat_id": "c1", "entry": "p-tide-log"},
                                      {"Id": 3, "chat_id": "c2", "entry": "p-tide-log"}]}
        self.store = None

    def dispatch(self, route, body, timeout):
        self.calls.append((route, body))
        if route in self.fail:
            self.fail.discard(route)
            return {"ok": False, "error": "boom"}
        if route == "/project-info":
            return self.info
        return {"ok": True, "merged": True, "path": "/home/op/archive/tide-log", "golden": True,
                "archived": True, "stream": body.get("id"), "result": "ok", "note": ""}

    def rows(self, table, field, value):
        return [r for r in self.tables[table] if r.get(field) == value]

    def drop_row(self, table, rid):
        self.dropped.append((table, rid))
        self.tables[table] = [r for r in self.tables[table] if r["Id"] != rid]

    def delete(self):
        return D.Delete(
            dispatch=self.dispatch, op=FakeOP(self.calls), rows=self.rows, drop_row=self.drop_row,
            initiative=lambda pid, name: self.init,
            file_gate=lambda pid, text: self.gates.append((pid, text)) or "run1",
            gate_status=lambda rid: self.status,
            refresh=lambda chat_id, pid: self.refreshed.append((chat_id, pid)) or "",
            drop_state=lambda pid: self.state_dropped.append(pid))

    def say(self, text, bound="", chat_id="c1"):
        saves = []

        def save(st):
            self.store = json.loads(json.dumps(st))
            saves.append(self.store)
        out = "".join(D.turn(text, bound, chat_id, self.delete(),
                             lambda: json.loads(json.dumps(self.store)) if self.store else None, save))
        return out

    def routes(self):
        return [c[0] for c in self.calls if c[0].startswith("/") and c[0] != "/project-info"]


def _approved(w):
    w.status = "executed"


def test_the_command_lists_what_will_go_and_files_a_gate_run_but_runs_nothing():
    w = World()
    out = w.say("delete project tide-log")
    assert "Delete project `tide-log`?" in out and "run `run1`" in out and "Nothing has run" in out
    assert "/home/op/tide-log" in out and "/home/op/archive/tide-log" in out
    assert "Initiative #1183" in out and "#tide-log" in out and "golden settings file" in out
    assert "1 row in `projects`, 1 row in `owui_new_projects`, 2 chat bindings in `owui_chats`" in out
    assert w.routes() == [] and w.dropped == [] and w.refreshed == [] and len(w.gates) == 1
    assert w.store["stage"] == "gate" and w.store["run_id"] == "run1"
    assert "✓" not in out


def test_asking_again_before_approval_runs_nothing_and_does_not_file_a_second_gate():
    w = World()
    w.say("delete project tide-log")
    out = w.say("delete project tide-log")
    assert "Still waiting for your approval" in out and len(w.gates) == 1
    assert w.routes() == [] and w.dropped == []


def test_after_approval_the_steps_run_in_order_each_reported_from_its_result():
    w = World()
    w.say("delete project tide-log")
    _approved(w)
    out = w.say("delete project tide-log")
    assert w.routes() == ["/archive-project", "/unregister-project", "/archive-board", "/archive-stream"]
    assert w.calls[1] == ("/archive-project", {"id": "tide-log", "confirm_remote": False})
    move = next(c for c in w.calls if c[0] == "move")
    assert move[1:4] == ("nomad-portfolio", 1183, "Won't do")
    for n in range(1, 7):
        assert f"✓ {n}/6" in out
    assert "moved to `/home/op/archive/tide-log`" in out
    assert "golden file removed" in out and "board `tide-log` archived" in out
    assert "Initiative #1183 moved to Won't do" in out and "#tide-log archived" in out
    assert "removed 1 row from projects, 1 row from owui_new_projects, 2 chat bindings" in out
    assert w.dropped == [("projects", 4), ("owui_new_projects", 7), ("owui_chats", 2), ("owui_chats", 3)]
    assert w.refreshed == [("c1", "tide-log")] and w.state_dropped == ["tide-log"]   # last of all
    assert "is deleted" in out and "/home/op/archive/tide-log" in out
    assert w.tables["owui_new_projects"] == [{"Id": 8, "chat_id": "del:c1", "project": "tide-log"}]


def test_the_chat_that_is_bound_to_the_project_may_leave_the_id_out():
    w = World()
    out = w.say("delete project", bound="tide-log")
    assert "Delete project `tide-log`?" in out
    assert "Which project?" in World().say("delete project")


def test_a_failed_step_stops_there_says_so_and_the_retry_resumes_without_twins():
    w = World(fail={"/archive-board"})
    w.say("delete project tide-log")
    _approved(w)
    out = w.say("delete project tide-log")
    assert "✓ 1/6" in out and "✓ 2/6" in out
    assert "⚠ 3/6 nomad-pm board failed: /archive-board: boom" in out
    assert "✓ 4/6" not in out and "is deleted" not in out
    assert w.store["stage"] == "running" and w.store["steps"] == ["repo", "registry"]
    assert w.routes() == ["/archive-project", "/unregister-project", "/archive-board"]
    assert w.dropped == [] and w.state_dropped == []
    out = w.say("delete project tide-log")
    assert out.startswith("✓ 3/6") and "✓ 6/6" in out and "✓ 1/6" not in out
    assert w.routes() == ["/archive-project", "/unregister-project", "/archive-board",
                          "/archive-board", "/archive-stream"]
    assert sum(1 for c in w.calls if c[0] == "move") == 1


def test_a_failure_after_the_board_is_archived_does_not_archive_it_or_close_the_initiative_twice():
    w = World()
    w.say("delete project tide-log")
    _approved(w)
    boom = {"n": 0}
    real = w.dispatch

    def flaky(route, body, timeout):
        if route == "/archive-stream" and not boom["n"]:
            boom["n"] = 1
            return {"ok": False, "error": "Must be an organization administrator"}
        return real(route, body, timeout)
    w.dispatch = flaky
    out = w.say("delete project tide-log")
    assert "⚠ 4/6 Zulip stream failed: /archive-stream: Must be an organization administrator" in out
    out = w.say("delete project tide-log")
    assert out.startswith("✓ 4/6") and "is deleted" in out
    assert w.routes().count("/archive-board") == 1 and sum(1 for c in w.calls if c[0] == "move") == 1


def test_a_step_that_raises_is_reported_not_hidden():
    w = World()
    w.say("delete project tide-log")
    _approved(w)
    w.tables["projects"] = None                           # NocoDB breaks at step 5
    out = w.say("delete project tide-log")
    assert "✓ 4/6" in out and "⚠ 5/6 Mission control failed: TypeError" in out and "is deleted" not in out


def test_unhonest_or_missing_approval_runs_nothing():
    for status, word in (("declined", "declined"), ("withdrawn", "withdrawn"), (None, "gone")):
        w = World()
        w.say("delete project tide-log")
        w.status = status
        out = w.say("delete project tide-log")
        assert "was not approved" in out and word in out and "Nothing was deleted" in out
        assert w.routes() == [] and w.dropped == [] and w.store["stage"] == "cancelled"
        again = w.say("delete project tide-log")           # a fresh request is a fresh gate
        assert "Delete project `tide-log`?" in again and len(w.gates) == 2


def test_refusals_file_no_gate_and_change_nothing():
    cases = [({"refuse": "`nomad` is a hub repo, and a chat never deletes a hub repo."}, "hub repo"),
             ({"known": False, "refuse": "`tide-log` is not in projects.yaml."}, "not in projects.yaml"),
             ({"refuse": "`tide-log` shares the board `civis` with other projects, so it is not deleted from a chat."},
              "shares the board")]
    for patch, word in cases:
        w = World(info={**INFO, **patch})
        out = w.say("delete project tide-log")
        assert word in out and "Nothing was changed" in out
        assert w.gates == [] and w.routes() == [] and w.store is None
    w = World()
    w.info = {"ok": False, "error": "dispatcher link error"}
    assert "can't check" in w.say("delete project tide-log") and w.gates == []
    assert "not a project id" in World().say("delete project Bad_Id!") and World().gates == []


def test_a_project_with_a_git_remote_needs_a_separate_confirmation_and_the_remote_is_never_touched():
    w = World(info={**INFO, "remote": ["git@github.com:x/tide-log.git"]})
    out = w.say("delete project tide-log")
    assert "git remote" in out and "confirm remote" in out and w.gates == [] and w.routes() == []
    out = w.say("delete project tide-log confirm remote")
    assert "Delete project `tide-log`?" in out and "is never touched" in out and len(w.gates) == 1
    assert w.store["plan"]["confirm_remote"] is True
    _approved(w)
    out = w.say("delete project tide-log")
    assert w.calls[[c[0] for c in w.calls].index("/archive-project")][1]["confirm_remote"] is True
    assert "git remote (git@github.com:x/tide-log.git) was not touched" in out
    assert not any("github" in c[0] or c[0] in ("push", "remote") for c in w.calls)


def test_one_delete_at_a_time_in_a_chat():
    w = World()
    w.say("delete project tide-log")
    out = w.say("delete project other-one")
    assert "waiting for approval" in out and "tide-log" in out and len(w.gates) == 1


def test_parse():
    assert D.parse("delete project tide-log") == ("tide-log", False)
    assert D.parse("  Delete Project `Tide-Log`, please") == ("tide-log", False)
    assert D.parse("delete project tide-log confirm remote") == ("tide-log", True)
    assert D.parse("delete project") == ("", False)
    assert D.parse("please delete project x") is None and D.parse("what about deleting it") is None
    assert D.parse("deleted project list") is None


# ── the chat path and the engine's gate ─────────────────────────────────────────────────

def _chat(world, deps_delete=True):
    rows = [{"id": "tide-log", "name": "Tide Log"}]
    seen = []

    def llm(body):
        seen.append(body)
        raise AssertionError("a delete command must not reach a model")
    d = O.Deps(registry=lambda: rows, fetch_context=lambda pid: {}, search=lambda **k: "",
               complete=lambda *a: seen.append(a) or "", stream=llm, recall=lambda q, s: [],
               remember=lambda s, r, c: None, log=lambda row: None,
               delete=(lambda text, bound, chat_id: world_turn(world, text, bound, chat_id))
               if deps_delete else None)
    return d, seen


def world_turn(w, text, bound, chat_id):
    return D.turn(text, bound, chat_id, w.delete(),
                  lambda: json.loads(json.dumps(w.store)) if w.store else None,
                  lambda st: setattr(w, "store", json.loads(json.dumps(st))))


def test_the_chat_routes_only_the_typed_command_and_never_asks_a_model():
    w = World()
    d, seen = _chat(w)
    events = list(O.turn({"entry": "p-tide-log", "chat_id": "c1", "messages": [
        {"role": "user", "content": "delete project tide-log"}]}, d))
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "Delete project `tide-log`?" in text and events[-1]["routed"] == "delete-project"
    assert seen == [] and w.routes() == []
    # in the bound chat, the id may be left out
    w2 = World()
    d2, _ = _chat(w2)
    events = list(O.turn({"entry": "p-tide-log", "chat_id": "c1", "messages": [
        {"role": "user", "content": "delete project"}]}, d2))
    assert "Delete project `tide-log`?" in "".join(e["text"] for e in events if e["type"] == "delta")


def test_without_the_hook_or_for_other_words_the_turn_is_unchanged():
    d, seen = _chat(World(), deps_delete=False)
    events = list(O.turn({"entry": "p-tide-log", "chat_id": "c1", "messages": [
        {"role": "user", "content": "delete project tide-log"}]}, d))
    assert events[-1].get("routed") != "delete-project"          # went on to the router as before
    d, _ = _chat(World())
    list(O.turn({"entry": "p-tide-log", "chat_id": "c1", "messages": [
        {"role": "user", "content": "how do I delete project files?"}]}, d))


def test_approve_in_chat_still_goes_to_the_gate_not_the_delete_hook():
    w = World()
    d, _ = _chat(w)
    d.gate = lambda decision, all_: "gate says " + decision
    events = list(O.turn({"entry": "p-tide-log", "chat_id": "c1", "messages": [
        {"role": "user", "content": "approve"}]}, d))
    assert "gate says approved" in "".join(e["text"] for e in events if e["type"] == "delta")
    assert w.gates == [] and w.routes() == []


class FakeDB:
    def __init__(self):
        self.rows = {"goals": [], "comms": [], "episodic": []}

    def create(self, table, fields):
        self.rows[table].append({"Id": len(self.rows[table]) + 1, **fields})
        return self.rows[table][-1]

    def find(self, table, field, value):
        return next((r for r in self.rows[table] if r.get(field) == value), None)

    def update(self, table, rid, fields):
        self.find(table, "Id", rid).update(fields)


def test_the_engine_files_a_gate_run_and_approving_it_changes_nothing_by_itself():
    import engine
    db = FakeDB()
    eng = engine.Engine(db=db, pm="nocodb")
    st = eng.file_gate("Delete project tide-log", D.ACTION, {"project": "tide-log", "summary": "s"},
                       target="tide-log", preview="the plan")
    row = db.find("comms", "run_id", st["run_id"])
    assert row["status"] == "awaiting-approval" and row["type"] == "proposal"
    proposal = json.loads(row["payload"])
    assert proposal["action"] == "delete_project" and proposal["args"]["project"] == "tide-log"
    # an approval needs the hash of exactly this proposal, like every gate run
    assert eng.resume_run(st["run_id"], "approved")["error"].startswith("expect_hash is required")
    res = eng.resume_run(st["run_id"], "approved", engine.proposal_hash(proposal))
    assert res["status"] == "executed" and "approved for tide-log" in res["outcome"]
    assert db.find("comms", "run_id", st["run_id"])["status"] == "executed"
    st2 = eng.file_gate("Delete project x", D.ACTION, {"project": "x"})
    assert eng.resume_run(st2["run_id"], "rejected")["status"] == "declined"


def test_a_project_with_no_initiative_says_so_and_closes_nothing():
    w = World(initiative=None)
    out = w.say("delete project tide-log")
    assert "no Initiative found in nomad-portfolio" in out
    _approved(w)
    out = w.say("delete project tide-log")
    assert "no Initiative in nomad-portfolio to close" in out and not any(c[0] == "move" for c in w.calls)


def test_a_board_that_was_not_in_openproject_is_reported_as_such():
    w = World()
    w.say("delete project tide-log")
    _approved(w)
    real = w.dispatch
    w.dispatch = lambda r, b, t: {"ok": True, "archived": False, "merged": True} if r == "/archive-board" \
        else real(r, b, t)
    out = w.say("delete project tide-log")
    assert "was not in OpenProject, nothing to archive" in out and "✓ 6/6" in out
