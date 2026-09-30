"""PM-01 P4 · following OpenProject: webhook → event, Zulip reply → comment, the board in numbers.

Offline, over a fake board. test_pm_follow_live.py proves the same against a real OpenProject that
delivers real signed webhooks.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pm_board  # noqa: E402
import pm_follow  # noqa: E402
from pm_openproject import PMError  # noqa: E402

OPERATOR = 4
CLOSED = {"Done", "Won't do", "Duplicate"}
NAMES = {"/api/v3/users/4": "OpenProject Admin", "/api/v3/users/6": "Research (agent)",
         "/api/v3/users/9": "Dev (agent)", "/api/v3/users/10": "Manager (agent)"}


def wp(i, subject, type_="Task", status="Ready", parent=None, lane="research",
       created="2026-09-01T10:00:00Z", updated="2026-09-01T10:00:00Z", lock=0):
    links = {"status": {"title": status}, "type": {"title": type_}, "project": {"title": "Work"},
             "parent": {"href": f"/api/v3/work_packages/{parent}" if parent else None}}
    return {"id": i, "subject": subject, "_links": links, "lane": lane, "createdAt": created,
            "updatedAt": updated, "lockVersion": lock}


def act(i, user, details=(), comment="", at="2026-09-01T10:00:00Z"):
    return {"id": i, "createdAt": at, "_links": {"user": {"href": f"/api/v3/users/{user}"},
                                                  "workPackage": {"href": "/api/v3/work_packages/0"}},
            "details": [{"raw": d} for d in details], "comment": {"raw": comment}}


class FakeOP:
    project_ident = "nomad-work"

    def __init__(self):
        self.wps, self.acts, self.watchers, self.rels, self.comments = {}, {}, {}, [], []
        self.hidden = set()
        self.moves = []

    def get(self, i):
        if i in self.hidden:
            raise PMError("GET -> 404")
        return self.wps[i]

    def activities(self, i):
        return self.acts.get(i, [])

    def watcher_ids(self, i):
        return self.watchers.get(i, [])

    def user_name(self, href):
        return NAMES.get(href)

    def lane_of(self, w):
        return w.get("lane")

    def closed(self, s):
        return s in CLOSED

    def comment(self, i, text, as_=None):
        self.comments.append((i, text, as_))
        return {"id": 900 + len(self.comments)}

    def move(self, i, target, comment="", as_=None):
        self.moves.append((i, target, as_))
        self.wps[i]["_links"]["status"]["title"] = target
        return self.wps[i]

    def project_wps(self):
        return list(self.wps.values())

    def block_relations(self):
        return list(self.rels)


def sign(secret, body):
    return "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def hook_wp(i, action="work_package:updated"):
    return {"action": action, "work_package": {"id": i}}


def hook_comment(a, wp_id):
    a = json.loads(json.dumps(a))
    a["_links"]["workPackage"]["href"] = f"/api/v3/work_packages/{wp_id}"
    return {"action": "work_package_comment:comment", "activity": a}


# ── pure pieces ──
def test_signature():
    body = b'{"action":"work_package:updated"}'
    assert pm_follow.verify("s3cret", body, sign("s3cret", body))
    assert not pm_follow.verify("s3cret", body, sign("other", body))
    assert not pm_follow.verify("s3cret", body + b" ", sign("s3cret", body))
    assert not pm_follow.verify("", body, sign("", body))          # no secret is no webhook
    assert not pm_follow.verify("s3cret", body, None)


def test_status_change_and_mentions():
    assert pm_follow.status_change([{"raw": "Type set to Task"}, {"raw": "Status set to New"}]) == (None, "New")
    assert pm_follow.status_change([{"raw": "Status changed from Ready to Blocked"}]) == ("Ready", "Blocked")
    assert pm_follow.status_change([{"raw": "**Status** changed from *Won't do* to *Backlog*"}]) == ("Won't do", "Backlog")
    assert pm_follow.status_change([{"raw": "Subject changed from a to b"}]) is None
    raw = ('hi <mention class="mention" data-id="4" data-type="user" data-text="@Admin">@Admin</mention> and '
           '<mention class="mention" data-id="7" data-type="group" data-text="@Team">@Team</mention>')
    assert pm_follow.mentioned_users(raw) == {4}
    assert pm_follow.plain(raw) == "hi @Admin and @Team"


# ── webhook → event ──
@pytest.fixture
def board():
    op = FakeOP()
    op.wps[5] = wp(5, "Collect sources", status="Blocked", lane="research")
    op.acts[5] = [act(50, 6, ["Status set to New"], at="2026-09-01T09:00:00Z"),
                  act(51, 9, ["Status changed from Ready to Blocked"], "waiting on the API key")]
    op.watchers[5] = [OPERATOR, 10]
    return op, pm_follow.Follow(op, operator_id=OPERATOR)


def test_blocked_on_a_watched_item(board):
    op, f = board
    ev = f.ingest(hook_wp(5))
    assert ev["seq"] == 1 and ev["wp_id"] == 5 and ev["activity_id"] == 51
    assert (ev["from"], ev["to"], ev["blocked"], ev["watched"]) == ("Ready", "Blocked", True, True)
    assert ev["author"] == "Dev (agent)" and ev["by_agent"] and not ev["by_operator"]
    assert ev["comment"] == "waiting on the API key" and ev["lane"] == "research"


def test_one_activity_two_hooks_is_one_event(board):
    """A status change with a comment aggregates into one activity and fires both hooks."""
    op, f = board
    assert f.ingest(hook_wp(5))
    assert f.ingest(hook_comment(op.acts[5][-1], 5)) is None
    assert len(f.since()["events"]) == 1


def test_mention_and_field_edits(board):
    op, f = board
    op.wps[6] = wp(6, "Draft")
    op.acts[6] = [act(60, 6, ["Subject changed from a to b"])]
    assert f.ingest(hook_wp(6)) is None                       # nothing worth a message
    m = act(61, 6, [], '<mention class="mention" data-id="4" data-type="user" data-text="@A">@A</mention> look')
    ev = f.ingest(hook_comment(m, 6))
    assert ev["mention"] and not ev["watched"] and ev["comment"] == "@A look" and ev["to"] is None


def test_unreadable_project_is_dropped(board):
    op, f = board
    op.hidden.add(5)
    assert f.ingest(hook_wp(5)) is None and f.since()["dropped"] == 1
    assert f.ingest({"action": "project:created"}) is None


def test_zulip_reply_is_a_manager_comment_and_its_echo_is_marked(board):
    op, f = board
    r = f.post_comment(5, "use the staging key")
    assert r == {"ok": True, "wp_id": 5, "activity_id": 901}
    i, text, who = op.comments[0]
    assert i == 5 and who == "nomad-manager" and text.endswith("use the staging key")
    assert text.startswith(pm_follow.ZULIP_PREFIX)
    op.acts[5].append(act(901, 10, [], text))
    ev = f.ingest(hook_comment(op.acts[5][-1], 5))
    assert ev["origin"] == "zulip"
    with pytest.raises(ValueError):
        f.post_comment(5, "   ")


def test_a_late_hook_takes_every_unseen_change_but_no_old_history(board):
    """Two quick changes by two people: the first hook lands after both are in."""
    op, f = board
    op.acts[5] += [act(52, 4, ["Status changed from Blocked to Ready"], at="2026-09-01T10:01:00Z"),
                   act(53, 10, ["Status changed from Ready to Blocked"], "again", at="2026-09-01T10:02:00Z")]
    last = f.ingest(hook_wp(5))
    evs = f.since()["events"]
    assert [e["activity_id"] for e in evs] == [51, 52, 53] and last["activity_id"] == 53
    assert f.ingest(hook_wp(5)) is None                       # the hooks for 52 and 53 repeat
    op.wps[7] = wp(7, "Fresh")
    op.acts[7] = [act(70, 9, ["Status set to New"], at="2026-09-01T11:00:00Z")]
    ev = f.ingest(hook_wp(7))
    assert ev["action"] == "created" and ev["to"] == "New"   # a created hook that came as updated


def test_since_and_boot(board):
    op, f = board
    f.ingest(hook_wp(5))
    op.acts[5].append(act(52, 4, ["Status changed from Blocked to Ready"], "fixed"))
    f.ingest(hook_wp(5))
    s = f.since(1, f.boot)
    assert [e["activity_id"] for e in s["events"]] == [52] and s["events"][0]["by_operator"]
    # another boot (engine restarted): the relay's cursor means nothing, send everything held
    assert len(f.since(2, "stale")["events"]) == 2


# ── the board in numbers ──
def T(day, h=10):
    return f"2026-09-{day:02d}T{h:02d}:00:00Z"


@pytest.fixture
def numbers():
    pm_board._history.clear()
    op = FakeOP()
    op.wps[1] = wp(1, "Launch", "Feature", "In Progress", created=T(1))
    op.wps[2] = wp(2, "Build", "Epic", "In Progress", parent=1, created=T(1))
    op.wps[3] = wp(3, "A", status="Done", parent=2, lane="research", created=T(1), updated=T(3), lock=4)
    op.wps[4] = wp(4, "B", status="Done", parent=2, lane="dev", created=T(1), updated=T(10), lock=4)
    op.wps[5] = wp(5, "C", status="Blocked", parent=2, lane="dev", created=T(2))
    op.wps[6] = wp(6, "D", status="Ready", parent=2, lane="comms", created=T(8))
    op.wps[7] = wp(7, "E", status="In Progress", parent=2, lane="research", created=T(8))
    op.wps[8] = wp(8, "F", status="Won't do", parent=2, lane="comms", created=T(2), updated=T(4))
    op.acts[3] = [act(1, 6, ["Status set to New"], at=T(1)),
                  act(2, 6, ["Status changed from Ready to In Progress"], at=T(2, 10)),
                  act(3, 6, ["Status changed from Awaiting approval to Done"], at=T(3, 10))]
    op.acts[4] = [act(4, 9, ["Status changed from Ready to In Progress"], at=T(9, 8)),
                  act(5, 4, ["Status changed from Awaiting approval to Done"], at=T(10, 8))]
    op.acts[8] = [act(6, 10, ["Status changed from Ready to Won't do"], at=T(4))]
    op.rels = [(6, 5), (5, 7), (4, 6)]          # (blocker, blocked): D→C→E, and B (done) → D
    return op


def test_board_summary(numbers):
    op = numbers
    s = pm_board.build(op, now=dt.datetime(2026, 9, 11, 12, tzinfo=dt.timezone.utc))
    assert s["open"] == 5                     # Feature, Epic, C, D, E
    rows = {r["status"]: r for r in s["by_status"]}
    assert [r["status"] for r in s["by_status"]] == ["Ready", "In Progress", "Blocked"]
    assert rows["In Progress"]["types"] == {"Feature": 1, "Epic": 1, "Task": 1}
    # blocked chain: C is Blocked and waits on D; E waits on C, so E is in the chain list too
    by = {b["id"]: b for b in s["blocked"]}
    assert set(by) == {5, 7}
    assert [(c["id"], c["depth"]) for c in by[7]["chain"]] == [(5, 1), (6, 2)]
    assert by[5]["chain"] == [{"id": 6, "subject": "D", "status": "Ready", "depth": 1}]   # 4 is Done
    assert s["wip"] == {"dev": {"Blocked": 1}, "research": {"In Progress": 1}}
    assert s["cycle"] == {"dev": {"n": 1, "median_h": 24.0, "mean_h": 24.0},
                          "research": {"n": 1, "median_h": 24.0, "mean_h": 24.0}}
    weeks = {w["week"]: w for w in s["throughput"]}
    assert len(s["throughput"]) == pm_board.WEEKS
    assert weeks["2026-08-31"] == {"week": "2026-08-31", "agent": 1, "human": 0}    # A, by Research
    assert weeks["2026-09-07"] == {"week": "2026-09-07", "agent": 0, "human": 1}    # B, by the operator
    (b,) = s["burnup"]
    assert b["id"] == 1 and b["dates"][0] == "2026-09-01" and b["dates"][-1] == "2026-09-11"
    assert b["scope"][0] == 2 and b["scope"][-1] == 6
    assert b["done"][b["dates"].index("2026-09-03")] == 1          # A
    assert b["done"][b["dates"].index("2026-09-04")] == 2          # F, Won't do, closes scope
    assert b["done"][-1] == 3


def test_history_is_read_once_per_lock_version(numbers):
    op = numbers
    calls = []
    real = op.activities
    op.activities = lambda i: calls.append(i) or real(i)
    now = dt.datetime(2026, 9, 11, tzinfo=dt.timezone.utc)
    pm_board.build(op, now=now)
    first = len(calls)
    pm_board.build(op, now=now)
    assert first == 3 and len(calls) == first               # only closed tasks, and only once
    op.wps[3]["lockVersion"] += 1                           # reopened or edited → read again
    pm_board.build(op, now=now)
    assert len(calls) == first + 1


def test_reopened_task_is_not_closed_by_its_old_transition(numbers):
    op = numbers
    op.acts[3].append(act(9, 6, ["Status changed from Done to In Progress"], at=T(5)))
    started, closed_at, _ = pm_board.lifecycle(op, op.wps[3])
    assert started and closed_at is None


def test_imported_work_stays_out_of_cycle_time_and_throughput(numbers):
    op = numbers
    op._fields = lambda: {"Legacy ref": "customField9"}
    op.wps[3]["customField9"] = "nocodb:tasks/47"            # A was imported, not worked here
    s = pm_board.build(op, now=dt.datetime(2026, 9, 11, 12, tzinfo=dt.timezone.utc))
    assert set(s["cycle"]) == {"dev"}
    assert {w["week"]: w["agent"] for w in s["throughput"]}["2026-08-31"] == 0


# ── routes ──
def test_routes(monkeypatch, board):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server
    op, f = board
    monkeypatch.setattr(server, "_follow", f)
    bumps = []
    monkeypatch.setattr(server, "_bump_runs", lambda: bumps.append(1))
    c = TestClient(server.app)
    body = json.dumps(hook_wp(5)).encode()
    monkeypatch.setattr(pm_follow, "read_secret", lambda path=None: "")
    assert c.post("/pm-hook", content=body).status_code == 503
    monkeypatch.setattr(pm_follow, "read_secret", lambda path=None: "k")
    assert c.post("/pm-hook", content=body, headers={"X-OP-Signature": sign("x", body)}).status_code == 401
    r = c.post("/pm-hook", content=body, headers={"X-OP-Signature": sign("k", body)})
    assert r.status_code == 200 and r.json() == {"event": 1} and bumps == [1]
    ev = c.get("/pm-events", params={"after": 0, "boot": f.boot}).json()
    assert ev["boot"] == f.boot and [e["wp_id"] for e in ev["events"]] == [5]
    assert c.post("/pm-comment", json={"wp_id": 5, "text": "ok"}).json()["activity_id"] == 901
    assert c.post("/pm-comment", json={"wp_id": 5, "text": ""}).status_code == 400


# ---- approve or reject by comment (docs/prd/autopilot-approval.md 5.2) -----------------------

def test_decision_keywords():
    for text, want in [("approve", "Approved"), ("Approved: (a)", "Approved"),
                       ("APPROVE - go", "Approved"), ("approve\nkeep the HUD", "Approved"),
                       ("reject: too broad", "Changes requested"), ("Rejected", "Changes requested"),
                       ("reject\u2014split it", "Changes requested")]:
        assert pm_follow.decision(text) == want, text
    for text in ["Approve? Not sure yet", "approval later", "I approve", "rejection", "", "ok"]:
        assert pm_follow.decision(text) is None, text


def _awaiting(status="Awaiting approval"):
    op = FakeOP()
    op.wps[7] = wp(7, "Pick a HUD", status=status, lane="dev")
    return op, pm_follow.Follow(op, operator_id=OPERATOR)


def _comment(f, i, user, text):
    a = act(i, user, comment=text)
    a["_links"]["workPackage"]["href"] = "/api/v3/work_packages/7"
    return f.ingest({"action": "work_package_comment:comment", "activity": a})


def test_operator_comment_moves_an_awaiting_task_once():
    op, f = _awaiting()
    ev = _comment(f, 1, OPERATOR, "approve: (a)")
    assert ev["decided"] == "Approved" and op.moves == [(7, "Approved", pm_follow.MANAGER)]
    assert _comment(f, 1, OPERATOR, "approve: (a)") is None     # the same journal again
    assert len(op.moves) == 1


def test_zulip_reply_rejects():
    op, f = _awaiting()
    ev = _comment(f, 2, 10, pm_follow.ZULIP_PREFIX + "Reject: split it in two")
    assert ev["decided"] == "Changes requested"
    assert op.moves == [(7, "Changes requested", pm_follow.MANAGER)]


def test_no_move_from_an_agent_a_question_or_another_status():
    op, f = _awaiting()
    assert _comment(f, 3, 9, "approve")["decided"] is None                  # a worker
    assert _comment(f, 4, 10, "**Operator, via the cockpit:** approve")["decided"] is None
    assert _comment(f, 5, OPERATOR, "Approve? Not sure yet")["decided"] is None
    op2, f2 = _awaiting("In Progress")
    assert _comment(f2, 6, OPERATOR, "approve")["decided"] is None
    assert op.moves == [] and op2.moves == []


def test_a_refused_move_leaves_the_event():
    op, f = _awaiting()

    def refuse(*a, **k):
        raise PMError("422")
    op.move = refuse
    ev = _comment(f, 8, OPERATOR, "approve")
    assert ev and ev["decided"] is None
