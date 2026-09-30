"""PM-01 P5 · the one-time import: the mapping, and that a rerun is a no-op. Offline, over a fake."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pm_import  # noqa: E402

CF = {"Legacy ref": "customField9", "Notion ID": "customField10"}

GOALS = [{"Id": 1, "title": "Overview", "notion_id": "n-g1", "priority": "Low"},
         {"Id": 7, "title": "Civis", "description": "Build it"},
         {"Id": 9, "title": "Content"}]
MILESTONES = [{"Id": 1, "title": "Write it", "status": "In Progress", "goal_ref": None, "notion_id": "n-m1"},
              {"Id": 20, "title": "Discovery", "status": "Done", "goal_ref": "7"},
              {"Id": 22, "title": "Core build", "status": "Planned", "goal_ref": "7"},
              {"Id": 28, "title": "Shipped", "status": "Done", "goal_ref": "9"}]
TASKS = [{"Id": 1, "title": "v1 thing", "status": "Done", "assigned_agent": "Engineering Crew", "milestone_ref": None},
         {"Id": 8, "title": "v1 open", "status": "Backlog", "assigned_agent": "Researcher", "milestone_ref": None},
         {"Id": 47, "title": "Scope", "status": "Done", "assigned_agent": "research", "milestone_ref": "20",
          "output_link": "https://example.test/doc"},
         {"Id": 50, "title": "Dropped", "status": "Skipped", "assigned_agent": "dev", "milestone_ref": "20"},
         {"Id": 54, "title": "Build", "status": "Blocked", "assigned_agent": "dev", "milestone_ref": "22"},
         {"Id": 71, "title": "Post", "status": "Done", "assigned_agent": "comms", "milestone_ref": "28"}]


class FakeOP:
    """One project of a shared board: ids are global, like OpenProject's."""
    def __init__(self, ident, board):
        self.ident, self.board = ident, board

    def _fields(self):
        return CF

    def project_wps(self):
        return [w for w in self.board["wps"].values() if w["project"] == self.ident]

    def create(self, type_name, subject, parent=None, lane="manager", description="", fields=None):
        i = len(self.board["wps"]) + 1
        w = {"id": i, "project": self.ident, "subject": subject, "parent": parent, "lane": lane,
             "description": description, "_links": {"status": {"title": "New"}, "type": {"title": type_name}}}
        for k, v in (fields or {}).items():
            if v:
                w[CF[k]] = v
        self.board["wps"][i] = w
        self.board["log"].append(("create", i))
        return w

    def move(self, wp_id, target):
        self.board["wps"][wp_id]["_links"]["status"]["title"] = target
        self.board["log"].append(("move", wp_id))


def ops_for(items, board=None):
    board = board or {"wps": {}, "log": []}
    return {p: FakeOP(p, board) for p in {i["project"] for i in items} | {"nomad-portfolio"}}, board


def by_ref(board):
    return {w[CF["Legacy ref"]]: w for w in board["wps"].values()}


def test_rollup():
    assert pm_import.rollup(["Done", "Won't do"]) == "Done"
    assert pm_import.rollup(["Won't do"]) == "Won't do"
    assert pm_import.rollup(["Done", "Backlog"]) == "In Progress"
    assert pm_import.rollup(["Backlog", "Ready"]) == "Backlog"
    assert pm_import.rollup([]) == "Backlog"


def test_nocodb_mapping():
    items = pm_import.nocodb_plan(GOALS, MILESTONES, TASKS)
    it = {i["ref"]: i for i in items}
    refs = [i["ref"] for i in items]
    # parents always come before their children
    for i in items:
        if i["parent"]:
            assert refs.index(i["parent"]) < refs.index(i["ref"])
    assert it["nocodb:mc_goals/1"]["type"] == "Feature" and it["nocodb:mc_goals/1"]["notion"] == "n-g1"
    # v1 rows have no links: the orphan milestone and the orphan tasks land under goal 1
    assert it["nocodb:milestones/1"]["parent"] == "nocodb:mc_goals/1"
    assert it["nocodb:v1-tasks"]["parent"] == "nocodb:mc_goals/1"
    assert it["nocodb:tasks/1"]["parent"] == "nocodb:v1-tasks" and it["nocodb:tasks/1"]["lane"] == "dev"
    assert it["nocodb:tasks/8"]["lane"] == "research"
    assert it["nocodb:v1-tasks"]["status"] == "In Progress"
    # statuses: Skipped is Won't do, Planned is Backlog, and a goal rolls up its milestones
    assert it["nocodb:tasks/50"]["status"] == "Won't do"
    assert it["nocodb:milestones/22"]["status"] == "Backlog"
    assert it["nocodb:mc_goals/7"]["status"] == "In Progress"
    assert it["nocodb:mc_goals/9"]["status"] == "Done"
    assert "https://example.test/doc" in it["nocodb:tasks/47"]["description"]
    assert {i["project"] for i in items} == {"nomad-work"}


def test_repo_plan_shape():
    items = pm_import.repo_plan()
    it = {i["ref"]: i for i in items}
    assert len(it) == len(items)                               # refs are unique
    for i in items:
        if i["type"] == "Initiative":
            assert i["project"] == "nomad-portfolio" and not i["parent"]
        else:
            assert i["parent"] in it
        assert "Snapshot of ~/" in i["description"] and "authority" in i["description"]
    assert it["civis:R1"]["project"] == "civis" and it["civis:R1"]["parent"] == "civis:R"
    assert it["civis:R"]["parent"] == "civis:initiative"
    assert "refactoring-plan-v2.md" in it["civis:R1"]["description"]
    assert {i["project"] for i in items} == {"nomad-portfolio", "civis", "shift", "crypto"}


def test_apply_creates_walks_and_is_idempotent():
    items = pm_import.nocodb_plan(GOALS, MILESTONES, TASKS) + pm_import.repo_plan()
    ops, board = ops_for(items)
    first = pm_import.apply(ops, items, log=lambda *_: None)
    assert first["created"] == len(items) and first["kept"] == 0
    got = by_ref(board)
    assert got["nocodb:tasks/54"]["_links"]["status"]["title"] == "Blocked"
    assert got["nocodb:tasks/8"]["_links"]["status"]["title"] == "Backlog"
    assert got["nocodb:tasks/1"][CF["Legacy ref"]] == "nocodb:tasks/1"
    assert got["civis:R1"]["parent"] == got["civis:R"]["id"]
    assert got["civis:R"]["parent"] == got["civis:initiative"]["id"]     # across projects
    assert got["civis:initiative"]["project"] == "nomad-portfolio"
    # every create happens before the first move, so nothing lands under a closed parent
    kinds = [k for k, _ in board["log"]]
    assert "create" not in kinds[kinds.index("move"):]

    board["log"].clear()
    again = pm_import.apply(ops, items, log=lambda *_: None)
    assert again == {"created": 0, "moved": 0, "kept": len(items)}
    assert board["log"] == []


def test_a_broken_run_resumes():
    items = pm_import.repo_plan()
    ops, board = ops_for(items)
    real = FakeOP.move
    FakeOP.move = lambda self, i, t: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        pm_import.apply(ops, items, log=lambda *_: None)
    except RuntimeError:
        pass
    finally:
        FakeOP.move = real
    assert all(w["_links"]["status"]["title"] == "New" for w in board["wps"].values())
    counts = pm_import.apply(ops, items, log=lambda *_: None)
    assert counts["created"] == 0 and counts["moved"] == len(items)
    assert by_ref(board)["shift:next-1"]["_links"]["status"]["title"] == "In Progress"


def test_dry_run_writes_nothing():
    items = pm_import.repo_plan()
    ops, board = ops_for(items)
    counts = pm_import.apply(ops, items, write=False, log=lambda *_: None)
    assert counts["created"] == len(items) and board["wps"] == {}
