"""#1146 · the Open WebUI chat's tools (owui_tools.py, docs/prd/owui-sessions.md §6.3).

What they pin down:
  - the tiers: reads are the cockpit's read-only tools, four green nomad-pm writes, three gated
  - nothing that sends, publishes, deletes, pushes or spends is a tool, and nothing resumes a run
  - every gated tool, wired to a real Engine, leaves its run at awaiting-approval with nothing
    executed and no message sent (the M2 gate test)
  - green writes land on the chat's board, or the parent's board when a parent is named
  - a failing tool answers with an error, never raises

Run: python3 -m pytest v2/test_owui_tools.py -q
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import owui_tools  # noqa: E402
from test_pm_engine import _eng  # noqa: E402

RED = re.compile(r"send|publish|email|mail|post|delete|remove|push|spend|pay|approve|resume|"
                 r"reject|merge|deploy", re.I)


class FakeBoardOP:
    """The OpenProject calls the green tools make, recording the board each one used."""

    def __init__(self, board, log, wps):
        self.board, self.log, self.wps = board, log, wps

    def get(self, i):
        return self.wps[int(i)]

    def create(self, type_name, subject, parent=None, lane="dev", description=""):
        self.log.append(("create", self.board, type_name, subject, parent, lane))
        wp = {"id": 900 + len(self.log), "subject": subject, "lockVersion": 0,
              "_links": {"type": {"title": type_name}, "status": {"title": "New"},
                         "project": {"title": self.board, "href": f"/api/v3/projects/{self.board}"}}}
        self.wps[wp["id"]] = wp
        return wp

    def comment(self, i, text):
        self.log.append(("comment", self.board, i, text))

    def move(self, i, status, comment=""):
        self.log.append(("move", self.board, i, status))
        wp = self.wps[int(i)]
        wp["_links"]["status"] = {"title": status}
        return wp

    def call(self, method, path, body=None):
        self.log.append((method, self.board, path, body))
        wp = self.wps[int(path.rsplit("/", 1)[-1])]
        return {**wp, **{k: v for k, v in body.items() if k == "subject"}}


def _actions(board="civis", **kw):
    log = []
    wps = {944: {"id": 944, "subject": "Card UI", "lockVersion": 3,
                 "_links": {"type": {"title": "Epic"}, "status": {"title": "In Progress"},
                            "project": {"title": "Agent Town", "href": "/api/v3/projects/7"}}}}
    starts = []
    a = owui_tools.Actions(
        board=board, op=lambda b: FakeBoardOP(b, log, wps),
        capture=kw.get("capture") or (lambda g: starts.append(("capture", g)) or {"error": "x"}),
        run_task=kw.get("run_task") or (lambda i: starts.append(("task", i)) or {"error": "x"}),
        run_next=kw.get("run_next") or (lambda b: starts.append(("next", b)) or {"error": "x"}))
    return owui_tools.build(a), log, starts


def test_the_tiers_are_the_allowlist():
    tools, _, _ = _actions()
    by = {}
    for name, t in tools.items():
        by.setdefault(t["tier"], set()).add(name)
    assert by["read"] == set(owui_tools.READS)
    assert by["green"] == set(owui_tools.GREEN)
    assert by["gated"] == set(owui_tools.GATED)
    assert set(by) == {"read", "green", "gated"}


def test_no_red_tool_is_exposed():
    """Send / publish / email / post have no tool (they go through capture to the gate), and
    nothing deletes, pushes, spends or decides a gate."""
    tools, _, _ = _actions()
    assert [n for n in tools if RED.search(n)] == []
    for name in owui_tools.READS:
        assert tools[name]["tier"] == "read"


def test_every_gated_tool_stops_at_the_gate(monkeypatch):
    """The M2 gate test: each gated tool, against a real Engine, leaves a run awaiting approval,
    executes nothing and sends nothing."""
    e, db, op = _eng(monkeypatch)
    e.plan_project("P", "")
    t1 = op.by_subject("Find sources")
    sent = []
    e.tools = {"send_message": lambda **kw: sent.append(kw) or {"ok": True, "summary": "sent"}}
    a = owui_tools.Actions(board="1", op=lambda b: None, capture=e.start_run,
                           run_task=e.run_task,
                           run_next=lambda b: e.run_next_task(None, int(b)))
    tools = owui_tools.build(a)
    fix = op.create("Bug", "Card-flip flicker")["id"]                # New, no blocker
    results = [tools["run_next_task"]["call"]({}),                  # takes t1
               tools["build_task"]["call"]({"id": f"#{fix}"}),
               tools["capture"]["call"]({"goal": "Announce"})]       # a comms send, routed
    for r in results:
        assert r.get("status") == "awaiting-approval", r
        assert r["note"] == owui_tools.GATE_NOTE
    assert results[2]["lane"] == "comms"
    assert sent == []
    assert all(g.get("status") != "executed" for g in db.list("goals", 100))
    assert op.status(t1) == op.status(fix) == "Awaiting approval"


def test_a_gated_tool_that_starts_nothing_says_so():
    tools, _, starts = _actions(board="")
    assert "General" in tools["run_next_task"]["call"]({})["error"]
    assert tools["build_task"]["call"]({"id": "12"}) == {"error": "x"}
    assert starts == [("task", 12)]


def test_green_writes_land_on_the_chat_board_or_the_parents():
    tools, log, _ = _actions(board="civis")
    r = tools["pm_create"]["call"]({"type": "Bug", "subject": "Card-flip flicker", "parent": "#944"})
    assert log[-1] == ("create", "7", "Bug", "Card-flip flicker", 944, "dev")    # the epic's board
    assert r["created"]["url"].endswith(f"/work_packages/{r['created']['id']}")
    tools["pm_create"]["call"]({"type": "Task", "subject": "Docs"})
    assert log[-1][:2] == ("create", "civis")
    tools["pm_comment"]["call"]({"id": "944", "text": "hi"})
    assert log[-1] == ("comment", "civis", 944, "hi")
    assert tools["pm_transition"]["call"]({"id": 944, "status": "Done"})["moved"]["status"] == "Done"
    r = tools["pm_update"]["call"]({"id": "944", "subject": "Card UI v2"})
    assert log[-1][0] == "PATCH" and log[-1][3] == {"lockVersion": 3, "subject": "Card UI v2"}
    assert r["updated"]["subject"] == "Card UI v2"


def test_general_chat_needs_a_parent_to_create():
    tools, log, _ = _actions(board="")
    assert "General" in tools["pm_create"]["call"]({"type": "Task", "subject": "x"})["error"]
    assert log == []


def test_bad_arguments_and_failures_answer_with_an_error():
    tools, _, _ = _actions()
    assert "takes no argument" in tools["pm_comment"]["call"]({"id": 1, "text": "x", "as_": "me"})["error"]
    assert "needs text" in tools["pm_comment"]["call"]({"id": 1})["error"]
    assert "type must be" in tools["pm_create"]["call"]({"type": "Feature", "subject": "x"})["error"]
    assert "KeyError" in tools["pm_update"]["call"]({"id": 5, "subject": "x"})["error"]
    assert tools["pm_comment"]["call"]("nope") == {"error": "arguments must be an object"}


def test_openai_format():
    tools, _, _ = _actions()
    spec = {t["function"]["name"]: t["function"] for t in owui_tools.openai(tools)}
    assert set(spec) == set(tools)
    assert spec["pm_create"]["parameters"]["required"] == ["type", "subject"]
