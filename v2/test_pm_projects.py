"""#786 · every OpenProject project with its open tasks, for the cockpit's PROJECTS panel.

pm_board.projects() counts open Tasks, Bugs, User stories and Sub-tasks per project, by status and
by priority, and what "next" could take (#793), in five reads whatever the number of boards. These tests hold it to the PRD (R9, R10):
nomad-portfolio left out, closed work and containers not counted, Normal as the remainder, a
60 s cache that /pm-hook clears, and the last counts, marked stale, when OpenProject is down.

Run: python3 -m pytest v2/test_pm_projects.py -q   (engine deps live in the container)
"""
import json
import os
import sys
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pm_board  # noqa: E402
from pm_openproject import PMError  # noqa: E402

PROJECTS = [(14, "nomad-work", "NOMAD work"), (17, "civis", "Civis"),
            (18, "shift", "SHIFT"), (3, "nomad-portfolio", "NOMAD Portfolio")]
TYPES = {"Initiative": 9, "Feature": 4, "Epic": 5, "User story": 6, "Task": 1, "Bug": 7, "Sub-task": 11}
PRIO = {"Low": 7, "Normal": 8, "High": 9, "Immediate": 10}


def wp(i, project, status, priority="Normal", type_="Task"):
    return {"id": i, "project": project, "status": status, "priority": priority, "type": type_}


# What the board holds. The fake answers only for open statuses and counted types, the way
# OpenProject's "o" and type filters do, so a test that sees a Done or an Epic counted has caught
# the filters going missing, not the fake.
BOARD = [
    wp(1, 14, "Ready", "Immediate"), wp(2, 14, "Ready", "High"), wp(3, 14, "Blocked"),
    wp(4, 14, "Backlog"), wp(5, 14, "Done", "High"), wp(6, 14, "In Progress", type_="Epic"),
    wp(7, 17, "Awaiting approval", "High"), wp(8, 17, "New", "Low"), wp(9, 17, "In Review", type_="Bug"),
    wp(10, 3, "Ready"),
]
CLOSED = {"Done", "Won't do", "Duplicate"}


class FakeOP:
    def __init__(self, board=BOARD, blocks=()):
        self.board, self.blocks, self.fail, self.calls = list(board), list(blocks), False, []

    def _named(self, kind):
        return {"types": TYPES}[kind]

    def type_id(self, name):
        return TYPES[name]

    def _open(self, filters):
        want = next((f["type"]["values"] for f in filters if "type" in f), None)
        assert any("status" in f and f["status"]["operator"] == "o" for f in filters)
        return [w for w in self.board if w["status"] not in CLOSED
                and (want is None or str(TYPES[w["type"]]) in want)]

    def _all(self, filters, cap=2000, select=None):
        self.calls.append(("all", select))
        if self.fail:
            raise PMError("OpenProject unreachable")
        return [{"id": w["id"], "_links": {"project": {"href": f"/api/v3/projects/{w['project']}"},
                                           "status": {"title": w["status"]}, "type": {"title": w["type"]}}}
                for w in self._open(filters)]

    def block_relations(self):
        self.calls.append(("relations", None))
        if self.fail:
            raise PMError("OpenProject unreachable")
        return list(self.blocks)

    def call(self, method, path, body=None, **k):
        self.calls.append((method, path.split("?")[0]))
        if self.fail:
            raise PMError("OpenProject unreachable")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        if path.startswith("/projects"):
            return {"_embedded": {"elements": [{"id": i, "identifier": s, "name": n} for i, s, n in PROJECTS]}}
        if path.startswith("/priorities"):
            return {"_embedded": {"elements": [{"id": v, "name": k} for k, v in PRIO.items()]}}
        assert q["groupBy"] == ["project"] and q["pageSize"] == ["0"]
        filters = json.loads(q["filters"][0])
        pid = int(next(f["priority"]["values"][0] for f in filters if "priority" in f))
        name = {v: k for k, v in PRIO.items()}[pid]
        counts = {}
        for w in self._open(filters):
            if w["priority"] == name:
                counts[w["project"]] = counts.get(w["project"], 0) + 1
        return {"groups": [{"value": str(p), "count": n,
                            "_links": {"valueLink": [{"href": f"/api/v3/projects/{p}"}]}} for p, n in counts.items()]}


@pytest.fixture(autouse=True)
def clean():
    pm_board._projects.update(at=0.0, data=None)
    yield
    pm_board._projects.update(at=0.0, data=None)


def rows(op, **k):
    return {r["identifier"]: r for r in pm_board.projects(op, **k)["projects"]}


def test_one_row_per_project_except_the_portfolio():
    assert set(rows(FakeOP())) == {"nomad-work", "civis", "shift"}


def test_open_counts_skip_closed_work_and_containers():
    r = rows(FakeOP())
    assert r["nomad-work"]["open"] == 4          # 5 is Done, 6 is an Epic
    assert r["nomad-work"]["by_status"] == {"Ready": 2, "Blocked": 1, "Backlog": 1}
    assert r["civis"]["by_status"] == {"Awaiting approval": 1, "New": 1, "In Review": 1}


def test_a_project_with_nothing_open_is_still_listed_at_zero():
    s = rows(FakeOP())["shift"]
    assert s["open"] == 0 and s["by_status"] == {} and sum(s["by_priority"].values()) == 0


def test_priorities_split_the_open_count_and_normal_is_the_rest():
    r = rows(FakeOP())
    assert r["nomad-work"]["by_priority"] == {"Immediate": 1, "High": 1, "Normal": 2, "Low": 0}
    assert r["civis"]["by_priority"] == {"Immediate": 0, "High": 1, "Normal": 1, "Low": 1}
    for p in r.values():
        assert sum(p["by_priority"].values()) == p["open"]


def test_the_row_links_to_the_projects_work_package_list(monkeypatch):
    monkeypatch.setattr(pm_board, "PUBLIC_URL", "https://pm.example:8448")
    assert rows(FakeOP())["civis"]["url"] == "https://pm.example:8448/projects/civis/work_packages"


def test_five_reads_cover_every_board_and_the_list_is_trimmed():
    op = FakeOP()
    pm_board.projects(op)
    assert ("all", "id,project,status,type") in op.calls
    assert ("relations", None) in op.calls
    assert sum(1 for c in op.calls if c[1] == "/work_packages") == 3      # Immediate, High, Low
    assert len(op.calls) == 7          # + projects, priorities, the list, the relations


# ── what the per-project "next" could take (#793) ──

def test_pickable_counts_ready_and_backlog_tasks_and_bugs():
    r = rows(FakeOP())
    assert (r["nomad-work"]["pickable"], r["nomad-work"]["pickable_blocked"]) == (3, 0)   # 1, 2, 4
    assert (r["civis"]["pickable"], r["civis"]["pickable_blocked"]) == (0, 0)
    assert (r["shift"]["pickable"], r["shift"]["pickable_blocked"]) == (0, 0)


def test_pickable_skips_other_types_and_statuses():
    board = [wp(1, 18, "Ready", type_="User story"), wp(2, 18, "Ready", type_="Sub-task"),
             wp(3, 18, "New"), wp(4, 18, "Blocked"), wp(5, 18, "Done"), wp(6, 18, "Backlog", type_="Bug")]
    s = rows(FakeOP(board))["shift"]
    assert (s["pickable"], s["pickable_blocked"]) == (1, 0)


def test_an_open_blocker_holds_a_task_back_and_a_closed_one_does_not():
    board = [wp(1, 18, "Ready"), wp(2, 18, "Backlog"), wp(3, 18, "In Progress", type_="Epic"),
             wp(4, 18, "Done"), wp(5, 17, "Ready")]
    s = rows(FakeOP(board, blocks=[(3, 1), (4, 2)]))["shift"]
    assert (s["pickable"], s["pickable_blocked"]) == (1, 1)
    s = rows(FakeOP(board, blocks=[(3, 1), (5, 2)]), fresh=True)["shift"]   # a blocker on another board
    assert (s["pickable"], s["pickable_blocked"]) == (0, 2)


def test_the_cache_holds_for_a_minute_and_the_hook_clears_it():
    op = FakeOP()
    pm_board.projects(op, now=1000)
    n = len(op.calls)
    assert pm_board.projects(op, now=1059)["age"] == 59 and len(op.calls) == n
    pm_board.invalidate_projects()
    pm_board.projects(op, now=1060)
    assert len(op.calls) == 2 * n


def test_openproject_down_serves_the_last_counts_with_their_age():
    op = FakeOP()
    pm_board.projects(op, now=1000)
    op.fail = True
    d = pm_board.projects(op, now=1300)
    assert d["stale"] and d["age"] == 300 and "unreachable" in d["error"]
    assert {r["identifier"] for r in d["projects"]} == {"nomad-work", "civis", "shift"}


def test_openproject_down_with_nothing_cached_is_an_error():
    op = FakeOP()
    op.fail = True
    with pytest.raises(PMError):
        pm_board.projects(op)


def test_select_trims_each_element_to_the_named_links():
    from pm_openproject import OpenProject
    seen = []

    class OP(OpenProject):
        def call(self, method, path, body=None, **k):
            seen.append(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query))
            return {"_embedded": {"elements": []}, "total": 0}
    OP(base="http://x")._all([], select="id,project,status")
    assert seen[0]["select"] == ["total,elements/id,elements/project,elements/status"]
