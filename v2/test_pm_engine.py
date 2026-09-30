"""PM-01 P3 · the engine's project flow on OpenProject, offline.

NOMAD_PM=openproject moves projects from the NocoDB mc_goals/milestones/tasks tables to
OpenProject: Feature > Epic > Task. These tests drive the real Engine over a FakeDB and an
in-memory OpenProject that walks the same workflow the seed installs, so an illegal move fails
here the way it would there. No LLM, no network.

What they pin down:
  - the switch defaults to nocodb, and a run remembers which board its task came from
  - plan_project writes the tree, marks tasks Ready and turns `after` into blocks relations
  - run_next_task takes a Task or Bug with no open blocker: Ready before Backlog, then
    priority, then WP number (#792); a Backlog pick moves to Ready with a comment
  - gate states mirror: awaiting-approval, executed → Done, failed → Blocked with the error,
    declined → Won't do; each step comments as the lane's user
  - Epic and Feature close once everything under them is closed
  - an OpenProject outage never costs a run

Run: python3 -m pytest v2/test_pm_engine.py -q
"""
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine as engine_mod  # noqa: E402
from engine import Engine, proposal_hash  # noqa: E402
from pm_openproject import (CONTAINERS, FLOW_CONTAINER, FLOW_STANDARD, OpenProject,  # noqa: E402
                            PMError, path_to, project_id)
from test_engine_states import FakeDB  # noqa: E402

CLOSED = {"Done", "Won't do", "Duplicate"}
LANES = ["comms", "research", "support", "ads", "dev"]
ROUTES = {"Find sources": "research", "Write brief": "research", "Announce": "comms"}


class FakeOP:
    """The OpenProject calls the engine makes, in memory. Moves go one legal hop at a time."""

    tree = staticmethod(OpenProject.tree)

    def __init__(self):
        self.wps, self.rels, self.comments, self.moves = {}, [], [], []
        self.next_id, self.down = 100, False

    def _up(self):
        if self.down:
            raise PMError("OpenProject unreachable")

    def _wp(self, i):
        return self.wps[int(i)]

    HOME = 1          # the board NOMAD_PM_PROJECT names; the engine creates only there

    def create(self, type_name, subject, parent=None, lane="manager", description="", board=HOME):
        self._up()
        self.next_id += 1
        links = {"type": {"title": type_name}, "status": {"title": "New"},
                 "priority": {"title": "Normal"}, "project": {"href": f"/api/v3/projects/{board}"}}
        if parent:
            links["parent"] = {"href": f"/api/v3/work_packages/{int(parent)}"}
        wp = {"id": self.next_id, "subject": subject, "_links": links, "lane": lane,
              "run_id": None, "link": None}
        self.wps[wp["id"]] = wp
        return wp

    def get(self, i):
        self._up()
        return self._wp(i)

    def move(self, i, target, comment="", as_="nomad-manager"):
        self._up()
        wp = self._wp(i)
        flow = FLOW_CONTAINER if wp["_links"]["type"]["title"] in CONTAINERS else FLOW_STANDARD
        for step in path_to(flow, wp["_links"]["status"]["title"], target):
            self.moves.append((wp["id"], step, as_))
            wp["_links"]["status"]["title"] = step
        if comment:
            self.comment(wp["id"], comment, as_=as_)
        return wp

    def comment(self, i, text, as_="nomad-manager"):
        self._up()
        who = as_ if as_.startswith("nomad-") else f"nomad-{as_}"
        self.comments.append((int(i), who, text))

    def blocks(self, a, b):
        self._up()
        if a == b or (b, a) in self.rels:
            raise PMError("cycle")
        self.rels.append((int(a), int(b)))

    def open_blockers(self, i, by_id=None):
        return [a for a, b in self.rels if b == int(i) and not self.closed(self.status(a))]

    def set_run(self, i, run_id, link=""):
        self._up()
        self._wp(i).update(run_id=run_id, link=link)

    def project_wps(self, pid=None):
        self._up()
        on = f"/api/v3/projects/{pid or self.HOME}"
        return sorted((w for w in self.wps.values() if w["_links"]["project"]["href"] == on),
                      key=lambda w: w["id"])

    @staticmethod
    def closed(name):
        return name in CLOSED

    @staticmethod
    def lane_of(wp):
        return wp["lane"]

    # test helpers
    def status(self, i):
        return self._wp(i)["_links"]["status"]["title"]

    def by_subject(self, s):
        return next(w["id"] for w in self.wps.values() if w["subject"] == s)

    def said(self, i):
        return [(who, text) for w, who, text in self.comments if w == int(i)]


class Spec:
    def __init__(self, lane):
        self.lane = lane

    def propose(self, intent, target, run_id):
        return {"action": "send_message", "args": {"to": "operator", "body": intent}}


PLAN = {"milestones": [
    {"title": "Research", "tasks": [
        {"key": "t1", "title": "Find sources", "lane": "research", "after": []},
        {"key": "t2", "title": "Write brief", "lane": "research", "after": ["t1"]}]},
    {"title": "Launch", "tasks": [
        {"key": "t3", "title": "Announce", "lane": "comms", "after": ["t2", "nope"]}]}],
    "assumptions": ["the audience is the operator"], "risk": "no sources exist"}


def _eng(monkeypatch, plan=PLAN, route_lane=None, tool_ok=True, pm="openproject"):
    db, op = FakeDB(), FakeOP()
    for t in ("mc_goals", "milestones", "tasks"):
        db.t[t] = {}
    e = Engine(db=db, pm=pm, op=op)
    e.auto_advance = False            # tests step the project explicitly
    e.specialists = {l: Spec(l) for l in LANES}
    e.tools = {"send_message": lambda **kw: {"ok": tool_ok, "summary": "sent",
                                             "error": "" if tool_ok else "smtp down"}}

    def chat_json(model, system, user):
        if "Planner" in system:
            return json.loads(json.dumps(plan))
        goal = user.removeprefix("Goal: ")
        lane = route_lane or ROUTES.get(goal, "comms")
        return {"lane": lane, "intent": goal, "target": "operator", "confidence": 0.9}
    monkeypatch.setattr(engine_mod.llm, "chat_json", chat_json)
    monkeypatch.setattr(engine_mod.memory, "recall", lambda db, q: "")
    return e, db, op


def _approve(e, db, run_id):
    return e.resume_run(run_id, "approved",
                        proposal_hash(json.loads(db.find("comms", "run_id", run_id)["payload"])))


# ── the switch ──

def test_the_switch_defaults_to_nocodb(monkeypatch):
    monkeypatch.setattr(engine_mod, "PM_BACKEND", "nocodb")
    assert Engine(db=FakeDB()).pm == "nocodb"
    assert Engine(db=FakeDB(), pm="jira").pm == "nocodb"     # a typo falls back, it does not guess
    assert Engine(db=FakeDB(), pm="openproject").pm == "openproject"


def test_nocodb_plan_never_touches_openproject(monkeypatch):
    e, db, op = _eng(monkeypatch, pm="nocodb")
    r = e.plan_project("P", "")
    assert r["task_count"] == 3 and len(db.t["tasks"]) == 3
    assert op.wps == {}


# ── plan_project ──

def test_plan_writes_the_tree_ready_with_blocks(monkeypatch):
    e, db, op = _eng(monkeypatch)
    r = e.plan_project("Newsletter", "monthly")
    f = r["goal_id"]
    assert op.wps[f]["_links"]["type"]["title"] == "Feature"
    epics, tasks = op.tree(op.project_wps(), f)
    assert [x["subject"] for x in epics] == ["Research", "Launch"]
    assert all(op.status(t["id"]) == "Ready" for ts in tasks.values() for t in ts)
    assert [op.lane_of(t) for ts in tasks.values() for t in ts] == ["research", "research", "comms"]
    t1, t2, t3 = (op.by_subject(s) for s in ("Find sources", "Write brief", "Announce"))
    assert sorted(op.rels) == sorted([(t1, t2), (t2, t3)])
    assert r["relations"] == 2 and len(r["relation_errors"]) == 1 and "nope" in r["relation_errors"][0]
    assert db.t["mc_goals"] == {} and db.t["tasks"] == {}       # nothing on the NocoDB board
    note = op.said(f)[0][1]
    assert "the audience is the operator" in note and "no sources exist" in note


def test_plan_survives_a_cycle(monkeypatch):
    plan = {"milestones": [{"title": "M", "tasks": [
        {"key": "a", "title": "A", "lane": "dev", "after": ["b"]},
        {"key": "b", "title": "B", "lane": "dev", "after": ["a"]}]}]}
    e, db, op = _eng(monkeypatch, plan=plan)
    r = e.plan_project("Loop", "")
    assert r["task_count"] == 2 and r["relations"] == 1 and "cycle" in r["relation_errors"][0]


# ── run_next_task ──

def test_next_takes_only_ready_without_open_blockers(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f = e.plan_project("P", "")["goal_id"]
    t1, t2 = op.by_subject("Find sources"), op.by_subject("Write brief")
    r = e.run_next_task(f)
    assert r["task_id"] == t1 and r["status"] == "awaiting-approval"
    # t1 is in flight and t2/t3 are blocked by open work: nothing else may start
    assert e.run_next_task(f)["done"] is True
    _approve(e, db, r["run_id"])
    assert op.status(t1) == "Done"
    assert e.run_next_task(f)["task_id"] == t2


# ── the order "next" picks in (#792, PRD cockpit-remote-approval R11) ──

def _feature(op, *items):
    """A Feature > Epic holding one WP per (type, status, priority), created in the order given,
    so the WP numbers rise with it. Returns (feature id, [WP ids])."""
    f = op.create("Feature", "Ordering")["id"]
    ep = op.create("Epic", "All", parent=f)["id"]
    ids = []
    for n, (kind, status, prio) in enumerate(items):
        i = op.create(kind, f"Item {n}", parent=ep)["id"]
        op.move(i, status)
        op.wps[i]["_links"]["priority"]["title"] = prio
        ids.append(i)
    op.moves.clear()
    op.comments.clear()
    return f, ids


def _order(e, op, f):
    """Every WP "next" names, in turn, closing each one so the one after it comes up."""
    out = []
    while "task_id" in (n := e.next_task(f)):
        out.append(n["task_id"])
        op.wps[n["task_id"]]["_links"]["status"]["title"] = "Done"
    return out


def test_next_never_picks_a_task_with_an_open_blocker(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f, (a, b, blocker) = _feature(op, ("Task", "Ready", "Immediate"), ("Task", "Ready", "Low"),
                                  ("Task", "In Progress", "Normal"))
    op.blocks(blocker, a)
    assert e.next_task(f)["task_id"] == b          # a outranks b, but a is blocked
    op.wps[b]["_links"]["status"]["title"] = "Done"
    assert e.next_task(f)["done"] is True          # nothing left that is not blocked
    op.move(blocker, "Done")                       # a closed blocker no longer counts
    assert e.next_task(f)["task_id"] == a


def test_next_takes_only_tasks_and_bugs(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f, (story, sub, task, bug) = _feature(op, ("User story", "Ready", "Immediate"),
                                          ("Sub-task", "Ready", "Immediate"),
                                          ("Task", "Ready", "Low"), ("Bug", "Ready", "Low"))
    assert _order(e, op, f) == [task, bug]
    assert op.status(story) == "Ready" and op.status(sub) == "Ready"


def test_next_takes_ready_before_backlog_whatever_the_priority(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f, (back, ready) = _feature(op, ("Task", "Backlog", "Immediate"), ("Task", "Ready", "Low"))
    assert _order(e, op, f) == [ready, back]


def test_next_skips_new_and_in_flight_work(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f, (new, busy, back) = _feature(op, ("Task", "New", "Immediate"),
                                    ("Task", "In Progress", "Immediate"), ("Task", "Backlog", "Low"))
    assert _order(e, op, f) == [back]


def test_next_takes_immediate_then_high_then_normal_then_low(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f, (low, normal, high, now) = _feature(op, ("Task", "Ready", "Low"), ("Task", "Ready", "Normal"),
                                           ("Task", "Ready", "High"), ("Task", "Ready", "Immediate"))
    assert _order(e, op, f) == [now, high, normal, low]


def test_next_takes_the_lowest_wp_number_on_a_tie(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f, ids = _feature(op, *[("Task", "Ready", "High")] * 3)
    assert _order(e, op, f) == ids


def test_a_picked_backlog_task_moves_to_ready_with_a_comment(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f, (t,) = _feature(op, ("Bug", "Backlog", "Normal"))
    r = e.run_next_task(f)
    assert r["task_id"] == t and op.status(t) == "Awaiting approval"
    assert [s for w, s, _who in op.moves if w == t][:2] == ["Ready", "In Progress"]
    assert '"next"' in op.said(t)[0][1] and "Backlog" in op.said(t)[0][1]


def test_a_picked_ready_task_is_not_moved_back_through_ready(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f, (t,) = _feature(op, ("Task", "Ready", "Normal"))
    e.run_next_task(f)
    assert [s for w, s, _who in op.moves if w == t] == ["In Progress", "Awaiting approval"]
    assert not any('"next"' in text for _who, text in op.said(t))


def test_chat_and_briefing_next_name_what_run_next_task_takes(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f, (back, low, high, blocked) = _feature(op, ("Task", "Backlog", "Immediate"), ("Task", "Ready", "Low"),
                                             ("Bug", "Ready", "High"), ("Task", "Ready", "Immediate"))
    op.blocks(back, blocked)
    peek = e.next_task(f)
    assert peek == {"task_id": high, "task_title": "Item 2", "status": "Ready", "priority": "High"}
    assert e.next_task(f) == peek and op.moves == []        # reading it moves nothing
    assert e.run_next_task(f)["task_id"] == peek["task_id"]


def test_pick_next_asks_for_blockers_only_until_it_has_one():
    from pm_openproject import pick_next
    wps = [{"id": i, "_links": {"type": {"title": "Task"}, "status": {"title": "Ready"},
                                "priority": {"title": p}}}
           for i, p in ((1, "Low"), (2, "High"), (3, "Normal"), (4, None))]
    asked = []
    assert pick_next(wps, lambda i: asked.append(i) or i == 2)["id"] == 3
    assert asked == [2, 3]              # 4 has no priority: it ranks as Normal, so after 3 by number


def test_gate_mirror_and_lane_comments(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f = e.plan_project("P", "")["goal_id"]
    r = e.run_next_task(f)
    t = r["task_id"]
    assert op.status(t) == "Awaiting approval"
    assert op.wps[t]["run_id"] == r["run_id"] and r["run_id"] in op.wps[t]["link"]
    assert [s for w, s, _who in op.moves if w == t] == ["Ready", "In Progress", "Awaiting approval"]
    who = [w for w, _ in op.said(t)]
    assert who == ["nomad-manager", "nomad-research"]           # claim, then the routed lane
    payload = json.loads(db.find("comms", "run_id", r["run_id"])["payload"])
    assert payload["task_ref"] == t and payload["task_pm"] == "openproject"
    # the containers started with their first task
    epic = op.by_subject("Research")
    assert op.status(epic) == "In Progress" and op.status(f) == "In Progress"


def test_the_card_links_to_the_run_page_on_the_tailnet(monkeypatch):
    """#801: the card's Cockpit link opens the run page a phone can read, not the JSON of /run."""
    monkeypatch.setattr(engine_mod, "COCKPIT_PUBLIC_URL", "https://nomad.tail1234.ts.net:8449")
    e, db, op = _eng(monkeypatch)
    r = e.run_next_task(e.plan_project("P", "")["goal_id"])
    assert op.wps[r["task_id"]]["link"] == f"https://nomad.tail1234.ts.net:8449/cockpit/run/{r['run_id']}"


def test_without_a_tailnet_origin_the_link_uses_the_local_cockpit(monkeypatch):
    monkeypatch.setattr(engine_mod, "COCKPIT_PUBLIC_URL", engine_mod.COCKPIT_URL)
    assert engine_mod.run_page_url("abc123") == f"{engine_mod.COCKPIT_URL}/cockpit/run/abc123"
    assert "/run?id=" not in engine_mod.run_page_url("abc123")
    assert engine_mod.run_page_url("a/b?c").endswith("/cockpit/run/a%2Fb%3Fc")


def test_executed_is_done_and_containers_close(monkeypatch):
    plan = {"milestones": [{"title": "Only", "tasks": [{"key": "a", "title": "Announce",
                                                        "lane": "comms", "after": []}]}]}
    e, db, op = _eng(monkeypatch, plan=plan)
    f = e.plan_project("P", "")["goal_id"]
    r = e.run_next_task(f)
    assert _approve(e, db, r["run_id"])["status"] == "executed"
    t = r["task_id"]
    assert op.status(t) == "Done"
    assert op.said(t)[-1][0] == "nomad-comms" and "Executed" in op.said(t)[-1][1]
    assert op.status(op.by_subject("Only")) == "Done" and op.status(f) == "Done"
    s = e.project_status(f)
    assert (s["done"], s["total"], s["pct"]) == (1, 1, 100)


def test_failed_is_blocked_with_the_error(monkeypatch):
    e, db, op = _eng(monkeypatch, tool_ok=False)
    f = e.plan_project("P", "")["goal_id"]
    r = e.run_next_task(f)
    assert _approve(e, db, r["run_id"])["status"] == "failed"
    t = r["task_id"]
    assert op.status(t) == "Blocked"
    who, text = op.said(t)[-1]
    assert who == "nomad-research" and "smtp down" in text
    # retry puts the run and the task back at the gate
    assert e.retry_run(r["run_id"])["status"] == "awaiting-approval"
    assert op.status(t) == "Awaiting approval"


def test_declined_is_wont_do_and_does_not_advance(monkeypatch):
    e, db, op = _eng(monkeypatch)
    e.auto_advance = True
    started = []
    monkeypatch.setattr(e, "_advance", lambda gid, pm=None: started.append(gid))
    f = e.plan_project("P", "")["goal_id"]
    started.clear()
    r = e.run_next_task(f)
    assert e.resume_run(r["run_id"], "rejected")["status"] == "declined"
    assert op.status(r["task_id"]) == "Won't do"
    assert "rejected" in op.said(r["task_id"])[-1][1]
    assert started == []


def test_a_run_that_dies_before_the_gate_blocks_its_task(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f = e.plan_project("P", "")["goal_id"]

    def boom(**kw):
        raise RuntimeError("model timeout")
    e.specialists["research"].propose = boom
    try:
        e.run_next_task(f)
        raise AssertionError("expected the failure to surface")
    except RuntimeError as ex:
        assert "model timeout" in str(ex)
    t = op.by_subject("Find sources")
    assert op.status(t) == "Blocked" and "model timeout" in op.said(t)[-1][1]


def test_the_sweep_blocks_an_abandoned_task(monkeypatch):
    import datetime
    e, db, op = _eng(monkeypatch)
    f = e.plan_project("P", "")["goal_id"]
    t = op.by_subject("Find sources")
    op.move(t, "In Progress")
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=300)).isoformat()
    db.create("comms", {"run_id": "stale", "status": "new", "created_at": old,
                        "payload": json.dumps({"goal": "x", "task_ref": t, "task_pm": "openproject"})})
    assert len(e.sweep_stalled(45)) == 1
    assert op.status(t) == "Blocked"


def test_an_openproject_outage_never_costs_the_run(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f = e.plan_project("P", "")["goal_id"]
    r = e.run_next_task(f)
    op.down = True
    assert _approve(e, db, r["run_id"])["status"] == "executed"
    assert db.find("comms", "run_id", r["run_id"])["status"] == "executed"
    assert any(x["what"] == "openproject mirror failed" for x in db.t["episodic"].values())


def test_a_nocodb_run_is_not_mirrored_after_the_switch_flips(monkeypatch):
    """A task started on the NocoDB board finishes there even if NOMAD_PM changed meanwhile."""
    e, db, op = _eng(monkeypatch, pm="nocodb")
    g = e.plan_project("P", "")["goal_id"]
    r = e.run_next_task(g)
    e.pm = "openproject"
    _approve(e, db, r["run_id"])
    assert db.t["tasks"][r["task_id"]]["status"] == "Done"
    assert op.wps == {}


# ── a task on another board (#791) ──

CIVIS = 7


def _civis(op, tasks=("Draft the civis page",)):
    """A Feature > Epic > Ready Tasks tree on the civis board, beside the engine's own."""
    f = op.create("Feature", "Civis launch", board=CIVIS)["id"]
    ep = op.create("Epic", "Pages", parent=f, board=CIVIS)["id"]
    ts = [op.create("Task", s, parent=ep, lane="comms", board=CIVIS)["id"] for s in tasks]
    for t in ts:
        op.move(t, "Ready")
    op.moves.clear()
    return f, ep, ts


def test_a_civis_run_moves_the_civis_wp(monkeypatch):
    e, db, op = _eng(monkeypatch)
    home = e.plan_project("Home", "")["goal_id"]
    f, ep, (t,) = _civis(op)
    r = e.run_next_task(f)
    assert r["task_id"] == t and r["status"] == "awaiting-approval"
    assert op.status(t) == "Awaiting approval"
    assert op.wps[t]["run_id"] == r["run_id"] and r["run_id"] in op.wps[t]["link"]
    payload = json.loads(db.find("comms", "run_id", r["run_id"])["payload"])
    assert payload["task_ref"] == t and payload["task_pm"] == "openproject"
    assert op.status(ep) == "In Progress" and op.status(f) == "In Progress"
    assert _approve(e, db, r["run_id"])["status"] == "executed"
    assert op.status(t) == "Done"
    assert op.said(t)[-1][0] == "nomad-comms" and "Executed" in op.said(t)[-1][1]
    # the rollup reads the civis board: its Epic and Feature close
    assert op.status(ep) == "Done" and op.status(f) == "Done"
    s = e.project_status(f)
    assert (s["title"], s["done"], s["total"]) == ("Civis launch", 1, 1)
    # nothing on the engine's own board moved
    assert {w for w, _s, _who in op.moves} <= {t, ep, f}
    assert op.status(home) == "New"


def test_a_civis_task_blocks_retries_and_is_declined(monkeypatch):
    e, db, op = _eng(monkeypatch, tool_ok=False)
    f, ep, (t1, t2) = _civis(op, ("One", "Two"))
    r = e.run_next_task(f)
    assert r["task_id"] == t1
    assert _approve(e, db, r["run_id"])["status"] == "failed"
    assert op.status(t1) == "Blocked" and "smtp down" in op.said(t1)[-1][1]
    assert e.retry_run(r["run_id"])["status"] == "awaiting-approval"
    assert op.status(t1) == "Awaiting approval"
    assert e.resume_run(r["run_id"], "rejected")["status"] == "declined"
    assert op.status(t1) == "Won't do"
    # the Epic still has t2 open on the civis board, so it stays open
    assert op.status(ep) == "In Progress"
    assert e.run_next_task(f)["task_id"] == t2


def test_next_across_a_whole_board_uses_the_same_order(monkeypatch):
    e, db, op = _eng(monkeypatch)
    home, (loud,) = _feature(op, ("Task", "Ready", "Immediate"))       # another board: never picked
    f1, ep1, (a,) = _civis(op, ("A",))
    f2, ep2, (b,) = _civis(op, ("B",))
    op.wps[b]["_links"]["priority"]["title"] = "High"
    loose = op.create("Bug", "No parent", board=CIVIS)["id"]
    op.move(loose, "Backlog")
    op.wps[loose]["_links"]["priority"]["title"] = "Immediate"
    op.moves.clear()
    assert e.next_task(project=CIVIS)["task_id"] == b
    r = e.run_next_task(project=CIVIS)
    assert r["task_id"] == b and op.status(b) == "Awaiting approval"
    assert op.status(ep2) == "In Progress" and op.status(f2) == "In Progress"
    assert op.status(f1) == "New" and op.status(loud) == "Ready"
    assert e.next_task(project=CIVIS)["task_id"] == a
    op.wps[a]["_links"]["status"]["title"] = "Done"
    assert e.run_next_task(project=CIVIS)["task_id"] == loose              # no Epic above it: fine
    assert e.next_task(project=CIVIS)["done"] is True


def test_the_cockpits_next_on_civis_goes_to_the_gate_and_approval_closes_it_on_civis(monkeypatch):
    """#793: the per-project button sends {project} only, no Feature. The pick waits at the gate,
    and approving it moves the WP to Done on the civis board, not on the engine's own."""
    e, db, op = _eng(monkeypatch)
    home, (mine,) = _feature(op, ("Task", "Ready", "Immediate"))
    f, ep, (t,) = _civis(op)
    r = e.run_next_task(project=CIVIS)
    assert r["task_id"] == t and r["status"] == "awaiting-approval"
    assert op.status(t) == "Awaiting approval"
    assert _approve(e, db, r["run_id"])["status"] == "executed"
    assert op.status(t) == "Done" and project_id(op.wps[t]) == CIVIS
    assert op.status(ep) == "Done" and op.status(f) == "Done"
    assert op.status(mine) == "Ready" and op.status(home) == "New"


def test_a_civis_task_action_advances_its_own_feature(monkeypatch):
    e, db, op = _eng(monkeypatch)
    e.auto_advance = True
    started = []
    monkeypatch.setattr(e, "_advance", lambda gid, pm=None: started.append(gid))
    f, ep, (t,) = _civis(op)
    assert e.task_action(t, "skip")["status"] == "Won't do"
    assert op.status(ep) == "Done" and op.status(f) == "Done" and started == [f]


class _Calls(OpenProject):
    """The real client with its HTTP call recorded instead of sent."""

    def __init__(self, replies):
        super().__init__(project="nomad-work", base="http://op")
        self.replies, self.sent = replies, []

    def call(self, method, path, body=None, as_="nomad-manager", ok=(200, 201, 204)):
        self.sent.append((method, path, body))
        for prefix, reply in self.replies.items():
            if path.startswith(prefix):
                return reply
        raise AssertionError(f"unexpected {method} {path}")


def test_project_wps_names_the_board_and_defaults_to_nomad_pm_project():
    op = _Calls({"/projects/nomad-work": {"id": 3}, "/work_packages?": {"_embedded": {"elements": []}}})
    op.project_wps()
    op.project_wps(CIVIS)
    boards = [json.loads(urllib.parse.parse_qs(urllib.parse.urlsplit(p).query)["filters"][0])
              [0]["project"]["values"] for _m, p, _b in op.sent if p.startswith("/work_packages?")]
    assert boards == [["3"], [str(CIVIS)]]


def test_set_run_reads_the_fields_of_the_wps_own_board():
    schema = f"/api/v3/work_packages/schemas/{CIVIS}-1"
    op = _Calls({"/work_packages/55": {"id": 55, "lockVersion": 2,
                                       "_links": {"schema": {"href": schema},
                                                  "project": {"href": f"/api/v3/projects/{CIVIS}"}}},
                 schema: {"customField9": {"name": "Run ID"}, "customField10": {"name": "Cockpit link"}}})
    op.set_run(55, "abc", "http://cockpit/run/abc")
    method, path, body = op.sent[-1]
    assert (method, path) == ("PATCH", "/work_packages/55")
    assert body == {"lockVersion": 2, "customField9": "abc", "customField10": "http://cockpit/run/abc"}
    assert not any(p.startswith("/projects/") for _m, p, _b in op.sent)     # the home board is not asked


# ── task actions and the board views ──

def test_task_actions(monkeypatch):
    e, db, op = _eng(monkeypatch, tool_ok=False)
    f = e.plan_project("P", "")["goal_id"]
    r = e.run_next_task(f)
    _approve(e, db, r["run_id"])
    t = r["task_id"]
    assert e.task_action(t, "retry") == {"ok": True, "task_id": t, "status": "Ready"}
    assert op.status(t) == "Ready"
    assert e.task_action(t, "skip")["status"] == "Won't do"
    assert op.status(t) == "Won't do"
    assert "error" in e.task_action(t, "explode")


def test_goals_and_backlog(monkeypatch):
    e, db, op = _eng(monkeypatch)
    f1 = e.plan_project("One", "")["goal_id"]
    f2 = e.plan_project("Two", "")["goal_id"]
    goals = e.project_goals()
    assert [g["goal_id"] for g in goals] == [f2, f1]
    assert goals[0]["total"] == 3 and goals[0]["milestones"] == 2
    r = e.run_backlog(limit=5)
    assert r["started"] == 2 and {x["goal_id"] for x in r["runs"]} == {f1, f2}
