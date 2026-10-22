"""Tests for task qualification and ordering (#865, PRD 5.2). No live calls: the RuneMatch board
is a recording (fixtures/runematch_board.json, read-only, 2026-09-28) and the edge cases are
small hand-made boards in the same HAL shape.

Run: python3 -m pytest autopilot/ -q
"""
import json
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import state as st  # noqa: E402
import supervisor as sv  # noqa: E402
import tasks as tk  # noqa: E402

LANE = "customField4"


class RecordedClient:
    """The PMClient interface over a recorded (or hand-made) board. Counts reads."""

    def __init__(self, fx):
        self.fx = fx
        self.comment_reads = []

    @classmethod
    def load(cls, name="runematch_board.json"):
        with open(os.path.join(HERE, "fixtures", name)) as f:
            return cls(json.load(f))

    def open_work_packages(self, board):
        assert board == self.fx["board"]
        return [w for w in self.fx["work_packages"] if not self.closed(w["_links"]["status"]["title"])]

    def lane_key(self, board):
        return self.fx["lane_key"]

    def block_relations(self):
        return [tuple(r) for r in self.fx["block_relations"]]

    def work_package(self, wp_id):
        return self.fx["other_work_packages"][str(wp_id)]

    def closed(self, status):
        return status in self.fx["closed_statuses"]

    def comments(self, wp_id):
        self.comment_reads.append(wp_id)
        return self.fx["comments"].get(str(wp_id), [])

    def activities(self, wp_id):
        return self.fx.get("activities", {}).get(str(wp_id), [])

    def manager_id(self):
        return self.fx.get("manager_id")


def wp(i, status="Ready", type="Task", priority="Normal", lane="dev", subject=None,
       description="", category=None):
    links = {"status": {"title": status}, "type": {"title": type},
             "priority": {"title": priority}, LANE: {"title": lane}}
    if category:
        links["category"] = {"href": "/api/v3/categories/1", "title": category}
    return {"id": i, "subject": subject or f"task {i}", "updatedAt": "2026-09-28T00:00:00Z",
            "description": {"raw": description}, "_links": links}


def board(wps, relations=(), others=None, comments=None, activities=None, manager_id=None):
    return RecordedClient({"board": "b", "lane_key": LANE, "manager_id": manager_id,
                           "closed_statuses": ["Done", "Won't do", "Duplicate"],
                           "work_packages": wps, "block_relations": [list(r) for r in relations],
                           "other_work_packages": {str(k): v for k, v in (others or {}).items()},
                           "comments": {str(k): v for k, v in (comments or {}).items()},
                           "activities": {str(k): v for k, v in (activities or {}).items()}})


def pick(client, held=()):
    return tk.select(tk.fetch(client, client.fx["board"], held), held)


def reason(sel, i):
    return next((s.kind, s.reason) for s in sel.skipped if s.task.id == i)


# ---- the recorded RuneMatch board -----------------------------------------------------------

@pytest.fixture
def rm():
    return RecordedClient.load()


def test_runematch_picks_resume_first_then_board_order(rm):
    sel = pick(rm)
    assert [t.id for t in sel.qualified] == [833, 832, 842, 849]
    first = sel.qualified[0]
    assert first.resume and first.status == "In Progress"
    assert first.as_dict()["promote"] is False
    assert all(t.as_dict()["promote"] for t in sel.qualified[1:])     # New with criteria


def test_runematch_reasons(rm):
    sel = pick(rm)
    assert reason(sel, 647) == ("operator", "needs operator: Awaiting approval")
    assert reason(sel, 769)[0] == "operator"
    assert reason(sel, 648) == ("operator", "needs operator: Blocked with no open blocker")
    assert reason(sel, 625) == ("waiting", "blocked_by #631 (Blocked)")
    # a Done blocker (#624) is not listed, the open one is
    assert reason(sel, 628) == ("waiting", "blocked_by #627 (In Review)")
    assert reason(sel, 630) == ("person", "person-only: manager lane")
    assert reason(sel, 691)[0] == "person"                           # Ready, but manager lane
    assert reason(sel, 763) == ("criteria", "New without acceptance criteria")
    assert reason(sel, 621)[0] == "other"                            # Feature
    assert reason(sel, 843) == ("other", "In Progress without `autopilot: resume`")
    assert reason(sel, 627) == ("other", "status In Review")


def test_closed_offboard_blocker_does_not_block(rm):
    t = next(t for t in tk.fetch(rm, "runematch") if t.id == 842)
    assert [(b.id, b.status, b.closed) for b in t.blocked_by] == [(637, "Done", True)]
    assert tk.classify(t) is None


def test_comments_read_only_for_candidates(rm):
    tk.fetch(rm, "runematch")
    assert sorted(rm.comment_reads) == [763, 764, 832, 833, 842, 843, 849]


def test_held_task_is_not_returned(rm):
    sel = pick(rm, held=[833])
    assert 833 not in [t.id for t in sel.qualified]
    assert reason(sel, 833) == ("running", "held by a live worker")


# ---- qualification rules --------------------------------------------------------------------

AC = "Scope\n\n**Acceptance criteria**\n- it works\n"


def test_status_rules():
    sel = pick(board([wp(1, "Ready"), wp(2, "New", description=AC), wp(3, "New"),
                      wp(4, "Backlog", description=AC), wp(5, "In Review"),
                      wp(6, "Awaiting approval"), wp(7, "Blocked"), wp(8, "In Progress")],
                     comments={8: ["autopilot: started feat/8", "autopilot: resume\n\nhalf done"]}))
    assert [t.id for t in sel.qualified] == [8, 1, 2]
    assert [s.kind for s in sel.skipped] == ["criteria", "other", "other", "operator", "operator"]


def test_types():
    sel = pick(board([wp(1, type="Task"), wp(2, type="Bug"), wp(3, type="Sub-task"),
                      wp(4, type="Epic"), wp(5, type="Feature"), wp(6, type="User story")]))
    assert [t.id for t in sel.qualified] == [1, 2, 3]
    assert {s.task.id for s in sel.skipped} == {4, 5, 6}


def test_person_only_manager_lane_and_human_tag():
    sel = pick(board([wp(1, lane="manager"), wp(2, category="Human"),
                      wp(3, subject="[human] sign the contract"), wp(4, category="art")]))
    assert [t.id for t in sel.qualified] == [4]
    assert reason(sel, 1) == ("person", "person-only: manager lane")
    assert reason(sel, 2) == ("person", "person-only: tagged human")
    assert reason(sel, 3) == ("person", "person-only: tagged human")


def test_blockers_open_closed_and_offboard():
    b = board([wp(1), wp(2), wp(3), wp(4)], relations=[(1, 2), (9, 3), (8, 4)],
              others={9: wp(9, "Done"), 8: wp(8, "In Progress")})
    sel = pick(b)
    assert [t.id for t in sel.qualified] == [1, 3]
    assert reason(sel, 2) == ("waiting", "blocked_by #1 (Ready)")
    assert reason(sel, 4) == ("waiting", "blocked_by #8 (In Progress)")


def test_awaiting_approval_wins_over_blocker():
    sel = pick(board([wp(1), wp(2, "Awaiting approval")], relations=[(1, 2)]))
    assert reason(sel, 2)[0] == "operator"


# ---- ordering -------------------------------------------------------------------------------

def test_order_resume_then_priority_then_id():
    sel = pick(board([wp(1, priority="Low"), wp(2), wp(3, priority="High"), wp(4, priority="Immediate"),
                      wp(5, "In Progress", priority="Low"), wp(6, priority="High")],
                     comments={5: ["autopilot: result resume, tests left"]}))
    assert [t.id for t in sel.qualified] == [5, 4, 3, 6, 2, 1]


# ---- the heuristics -------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "**Acceptance criteria (PlayMode)**\n- a guide has separate head\n",
    "**Acceptance criteria:** WSEQ-15.\n",
    "## Acceptance\n\n1. the page loads\n",
    "Acceptance criteria:\n- x",
    "Done means: the digest arrives at 08:00",
    "### Definition of done\n- merged",
])
def test_has_acceptance_yes(text):
    assert tk.has_acceptance(text)


@pytest.mark.parametrize("text", [
    "", None,
    "Pending work from specs/ROADMAP-DEMO.md:1411.",
    "The acceptance of this depends on Josias.",
    "## Acceptance criteria\n\n## Notes\nlater",
    "**Acceptance criteria**\n",
])
def test_has_acceptance_no(text):
    assert not tk.has_acceptance(text)


def test_resume_is_the_latest_autopilot_comment():
    assert tk.resume_requested(["autopilot: started x", "autopilot: resume\nleft: tests"])
    assert tk.resume_requested(["autopilot: result resume"])
    assert tk.resume_requested(["autopilot: resume", "a human note"])
    assert not tk.resume_requested(["autopilot: resume", "autopilot: started feat/1"])
    assert not tk.resume_requested(["please resume this", "autopilot: promoted"])
    assert not tk.resume_requested([])


def test_board_mapping():
    assert tk.board_for("nomad") == "nomad-work"
    assert tk.board_for("civis-poc") == tk.board_for("civis-site") == "civis"
    assert tk.board_for("shift-poc") == "shift"
    assert tk.board_for("crypto-trading-bot") == "crypto"
    assert tk.board_for("content-creation-app") == "content-creation"
    assert tk.board_for("runematch") == "runematch"
    assert tk.board_for("nomad-public") is None


# ---- outcome ---------------------------------------------------------------------------------

def test_outcome():
    waiting = pick(board([wp(1, "In Review"), wp(2)], relations=[(1, 2)]))
    assert tk.outcome_for(waiting, []) == ("waiting", "#2 blocked_by #1 (In Review)")
    assert tk.outcome_for(waiting, [{"task": 1}]) is None
    both = pick(board([wp(1, "In Review"), wp(2), wp(3, "Awaiting approval")], relations=[(1, 2)]))
    assert tk.outcome_for(both, []) == ("needs_you", "#3 needs operator: Awaiting approval")
    person = pick(board([wp(1, lane="manager")]))
    assert tk.outcome_for(person, []) is None
    runnable = pick(board([wp(1), wp(2, "Awaiting approval")]))
    assert tk.outcome_for(runnable, []) is None


# ---- the Scheduler plug-in -------------------------------------------------------------------

class Entry:
    def __init__(self, autopilot):
        self.autopilot = autopilot


REG = {"runematch": Entry({"test": "x"}), "b": Entry({"test": "x"}), "plain": Entry(None),
       "nomad-public": Entry({"test": "x"})}


def test_known():
    s = tk.BoardScheduler(pm=RecordedClient.load(), registry=REG.get)
    assert s.known("runematch")
    assert not s.known("plain")          # no autopilot block
    assert not s.known("nomad-public")   # no board
    assert not s.known("nope")


def test_lanes_filter_a_project_to_its_own_lanes():
    tasks = tk.fetch(board([wp(1, lane="dev"), wp(2, lane="Comms"), wp(3, lane="")]), "b")
    sel = tk.select(tasks, lanes=("comms", "research"))
    assert [t.id for t in sel.qualified] == [2]                   # matching is case-insensitive
    assert reason(sel, 1) == ("lane", "lane dev is not this project's (comms, research)")
    assert reason(sel, 3) == ("lane", "lane (none) is not this project's (comms, research)")
    assert tk.outcome_for(sel, []) is None                        # another project's work


def test_no_lanes_configured_selects_as_before():
    b = board([wp(1, lane="dev"), wp(2, lane="comms"), wp(3, lane="")])
    assert [t.id for t in tk.select(tk.fetch(b, "b"), lanes=()).qualified] == [1, 2, 3]
    assert [t.id for t in pick(b).qualified] == [1, 2, 3]


def test_person_and_operator_reasons_win_where_they_did():
    sel = tk.select(tk.fetch(board([wp(1, lane="manager"), wp(2, "Awaiting approval", lane="comms")]),
                             "b"), lanes=("dev",))
    assert reason(sel, 1)[0] == "person"
    assert reason(sel, 2)[0] == "lane"        # another project's question, not this one's


def _ap(lanes=()):
    return types.SimpleNamespace(autopilot=types.SimpleNamespace(lanes=tuple(lanes)))


CC = {"content-creation": _ap(["comms", "research"]), "content-creation-app": _ap(["dev"]),
      "content-creation-backend": _ap(["dev"])}


def cc_board(wps):
    c = board(wps)
    c.fx["board"] = "content-creation"
    return c


def cc_select(pid, pm, registry=CC):
    s = tk.BoardScheduler(pm=pm, registry=registry.get)
    s.select_tasks(types.SimpleNamespace(project_id=pid, project={"workers": []}))
    return s.selections[pid]


def test_a_comms_task_on_the_shared_board_goes_to_content_creation_only():
    pm = cc_board([wp(221, lane="comms", subject="LinkedIn post")])     # #221, 2026-10-03
    app = cc_select("content-creation-app", pm)
    assert app.qualified == []
    assert reason(app, 221) == ("lane", "lane comms is not this project's (dev)")
    assert tk.outcome_for(app, []) is None
    assert [t.id for t in cc_select("content-creation-backend", pm).qualified] == []
    assert [t.id for t in cc_select("content-creation", pm).qualified] == [221]


def test_a_task_no_project_takes_is_reported_once():
    pm = cc_board([wp(5, lane="design"), wp(6, lane="dev")])
    first = cc_select("content-creation", pm)
    assert reason(first, 5)[0] == "unrouted"
    assert tk.outcome_for(first, []) == (
        "needs_you", "#5 unrouted: lane design, no project on this board takes it "
                     "(lanes: comms, dev, research)")
    for pid in ("content-creation-app", "content-creation-backend"):
        assert reason(cc_select(pid, pm), 5)[0] == "lane"


def test_a_project_without_lanes_on_the_board_takes_the_rest():
    reg = dict(CC, **{"content-creation-app": _ap()})
    pm = cc_board([wp(5, lane="design")])
    assert reason(cc_select("content-creation", pm, reg), 5)[0] == "lane"
    assert [t.id for t in cc_select("content-creation-app", pm, reg).qualified] == [5]


def test_board_projects():
    assert tk.board_projects("content-creation") == [
        "content-creation", "content-creation-app", "content-creation-backend"]
    assert tk.board_projects("civis") == ["civis", "civis-poc", "civis-site"]
    assert tk.board_projects("runematch") == ["runematch"]


class Recording(tk.BoardScheduler):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.started = []

    def start_workers(self, ctx, tasks):
        self.started += [t["task"] for t in tasks]
        return [{"task": t["task"]} for t in tasks]


def test_supervisor_pass_starts_in_order_and_skips_held(tmp_path):
    store = st.StateStore(str(tmp_path / "s.json"))
    store.set_autopilot("runematch", True)
    store.start("runematch")
    sch = Recording(pm=RecordedClient.load(), registry=REG.get)
    sup = sv.Supervisor(store, scheduler=sch)
    sup.run_pass("runematch")
    assert sch.started == [833, 832, 842]                     # per-project cap 3
    assert store.project("runematch")["state"] == "running"
    sup.run_pass("runematch")                                 # full: nothing new starts
    assert sch.started == [833, 832, 842]
    assert reason(sch.selections["runematch"], 833)[0] == "running"


def test_supervisor_pass_reports_waiting(tmp_path):
    store = st.StateStore(str(tmp_path / "s.json"))
    store.set_autopilot("b", True)
    store.start("b")
    pm = board([wp(1, "In Review"), wp(2)], relations=[(1, 2)])
    sup = sv.Supervisor(store, scheduler=Recording(pm=pm, registry=REG.get))
    sup.run_pass("b")
    p = store.project("b")
    assert (p["state"], p["detail"]) == ("waiting", "#2 blocked_by #1 (In Review)")


# ---- the live client's caching, against a fake engine module ----------------------------------

class FakeOP:
    PROJECT = "b"
    calls = []

    class OpenProject:
        def __init__(self, project):
            self.project_ident = project

        def project(self):
            return {"id": 1}

        def _all(self, filters):
            FakeOP.calls.append(("all", json.dumps(filters)))
            return [wp(1, "In Progress")]

        def _fields(self):
            return {"Lane": LANE}

        def block_relations(self):
            return []

        def closed(self, s):
            return s == "Done"

        def activities(self, i):
            FakeOP.calls.append(("activities", i))
            return [{"comment": {"raw": "autopilot: resume"}}, {"comment": None}]


def test_pmclient_reads_open_only_and_caches_comments_by_update():
    FakeOP.calls = []
    c = tk.PMClient(op_module=FakeOP)
    for _ in range(2):
        assert [t.resume for t in tk.fetch(c, "b")] == [True]
    assert [k for k, _ in FakeOP.calls] == ["all", "activities", "all"]
    assert '"operator": "o"' in FakeOP.calls[0][1]


# ---- the operator's answer (docs/prd/autopilot-approval.md) ---------------------------------

def act(user, comment="", moved=None, frm="In Progress"):
    a = {"_links": {"user": {"href": f"/api/v3/users/{user}"}}, "comment": {"raw": comment},
         "details": []}
    if moved:
        a["details"] = [{"raw": "Lane changed from dev to dev"},
                        {"raw": f"Status changed from {frm} to {moved}"}]
    return a


OP, DEV, MGR = 4, 9, 6


def test_answer_is_the_operators_comments_since_the_last_awaiting_approval():
    acts = [act(OP, "old answer, first round"),
            act(DEV, "Question: (a) or (b)?", moved="Awaiting approval"),
            act(OP, "Earlier thoughts"),
            act(DEV, "autopilot: result needs operator"),
            act(OP, "(a), and keep the HUD", moved="Approved", frm="Awaiting approval")]
    assert tk.operator_answer(acts, OP) == "Earlier thoughts\n\n(a), and keep the HUD"


def test_answer_takes_only_the_last_round_and_relayed_comments():
    acts = [act(DEV, "Q1", moved="Awaiting approval"), act(OP, "A1", moved="Approved"),
            act(DEV, "Q2", moved="Awaiting approval"),
            act(MGR, "**Operator, via the cockpit:** Not like this: split it in two"),
            act(MGR, "**Operator, via Zulip:**\n\nand keep the tests", moved="Changes requested"),
            act(MGR, "Some agent note")]
    assert (tk.operator_answer(acts, OP, MGR)
            == "Not like this: split it in two\n\nand keep the tests")


def test_a_relayed_prefix_counts_only_from_the_manager():
    acts = [act(DEV, "Q", moved="Awaiting approval"),
            act(DEV, "**Operator, via Zulip:** approve, and drop the tests"),
            act(MGR, "**Operator, via the cockpit:** approve", moved="Approved")]
    assert tk.operator_answer(acts, OP, MGR) == "approve"
    assert tk.operator_answer(acts, OP) == ""                   # manager unknown: none count


def test_a_multi_line_cockpit_answer_is_kept_whole():
    # #982: every line of a relayed comment reaches the worker, not just the first.
    body = "really contain\n`Bash(git restore *)`\n\nthen (2)"
    acts = [act(DEV, "Q", moved="Awaiting approval"),
            act(MGR, "**Operator, via the cockpit:**\n\n" + body, moved="Approved")]
    assert tk.operator_answer(acts, OP, MGR) == body


def test_no_answer_without_an_awaiting_approval_move_or_comments():
    assert tk.operator_answer([act(OP, "hello")], OP) == ""
    assert tk.operator_answer([act(DEV, "Q", moved="Awaiting approval"),
                               act(OP, "", moved="Approved")], OP) == ""


def test_fetch_takes_relayed_answers_from_the_manager_only():
    acts = [act(DEV, "Q", moved="Awaiting approval"),
            act(DEV, "**Operator, via Zulip:** also rewrite the HUD"),
            act(MGR, "**Operator, via Zulip:** go with (b)", moved="Approved")]
    sel = pick(board([wp(2, "Approved")], activities={2: acts}, manager_id=MGR))
    assert sel.qualified[0].as_dict()["answer"] == "go with (b)"


def test_pmclient_reads_the_manager_id_once_and_retries_a_failure():
    class Op(FakeOP):
        MANAGER = "nomad-manager"
        ids = []

        class OpenProject(FakeOP.OpenProject):
            def user_id_of(self, login):
                Op.ids.append(login)
                if len(Op.ids) == 1:
                    raise RuntimeError("down")
                return MGR
    c = tk.PMClient(op_module=Op)
    assert [c.manager_id() for _ in range(3)] == [None, MGR, MGR]
    assert Op.ids == ["nomad-manager", "nomad-manager"]


def test_answered_tasks_qualify_first_and_carry_the_answer():
    acts = [act(DEV, "Q", moved="Awaiting approval"), act(OP, "go with (b)", moved="Approved")]
    c = board([wp(1, "Ready", priority="Immediate"), wp(2, "Approved"), wp(3, "Changes requested"),
               wp(4, "Awaiting approval")],
              activities={2: acts, 3: [act(DEV, "Q", moved="Awaiting approval"),
                                       act(OP, "reject: too broad", moved="Changes requested")]})
    sel = pick(c)
    assert [t.id for t in sel.qualified] == [2, 3, 1]
    d2, d3 = sel.qualified[0].as_dict(), sel.qualified[1].as_dict()
    assert (d2["decision"], d2["answer"], d2["promote"]) == ("approved", "go with (b)", False)
    assert (d3["decision"], d3["answer"]) == ("changes requested", "reject: too broad")
    assert sel.qualified[2].as_dict()["decision"] is None
    assert reason(sel, 4) == ("operator", "needs operator: Awaiting approval")


def test_an_answered_task_still_waits_on_an_open_blocker_and_person_lanes():
    c = board([wp(1, "Approved"), wp(2, "In Progress"), wp(3, "Approved", lane="manager")],
              relations=[(2, 1)])
    sel = pick(c)
    assert sel.qualified == []
    assert reason(sel, 1) == ("waiting", "blocked_by #2 (In Progress)")
    assert reason(sel, 3) == ("person", "person-only: manager lane")
