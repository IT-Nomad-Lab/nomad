"""#1153 · + New project, the setup after `approve PRD` (owui_setup.py, docs/prd/owui-sessions.md §6.4
step 3).

What they pin down:
  - the steps run in order, each reported, each sent to the right place (dispatcher routes for
    the host-side ones, OpenProject for the roadmap, NocoDB for the console row)
  - a failed step stops the run, keeps what was done, and the next message resumes at that step:
    no step runs twice and no roadmap item is made twice
  - the roadmap: an Initiative in nomad-portfolio, a dated Feature for M1 under it, Epics and
    Tasks from the model's plan (or a starter plan when it isn't usable), the PRD path in each
  - a private or local-only project plans on private
  - nothing creates a GitHub remote or pushes

Run: python3 -m pytest v2/test_owui_setup.py -q
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import owui_newproject as N  # noqa: E402
import owui_setup as S  # noqa: E402

PLAN = json.dumps({"m1": "A tide table page", "weeks": 3, "epics": [
    {"title": "Data", "tasks": ["Set the test command in projects.yaml", "Add the tide API client"]},
    {"title": "Page", "tasks": ["Render the table"]}]})


class FakeOP:
    def __init__(self, log, wps):
        self.log, self.wps = log, wps

    def __call__(self, board):
        self.board = board
        return self

    def create(self, type_name, subject, parent=None, lane="manager", description=""):
        wid = 500 + len(self.wps)
        self.wps[wid] = {"id": wid, "lockVersion": 0}
        self.log.append(("create", self.board, type_name, subject, parent, description))
        return self.wps[wid]

    def get(self, wid):
        return self.wps[wid]

    def call(self, method, path, body=None):
        self.log.append((method, self.board, path, body))
        return {}


class World:
    """Every outside thing the steps touch, recording each call. `fail` names a route (or
    "roadmap:<n>" for the nth create) that fails once."""

    def __init__(self, plan=PLAN, fail=()):
        self.calls, self.wps, self.rows, self.refreshed, self.plans = [], {}, [], [], []
        self.fail, self.plan_text = set(fail), plan

    def dispatch(self, route, body, timeout):
        self.calls.append((route, body))
        if route in self.fail:
            self.fail.discard(route)
            return {"ok": False, "error": "boom"}
        return {"ok": True, "merged": True, "path": "/home/op/tide-log", "op_id": 9,
                "stream": body.get("id")}

    def setup(self):
        op = FakeOP(self.calls, self.wps)
        create = op.create

        def flaky(*a, **kw):
            n = sum(1 for c in self.calls if c[0] == "create") + 1
            if f"roadmap:{n}" in self.fail:
                self.fail.discard(f"roadmap:{n}")
                raise RuntimeError("OpenProject 503")
            return create(*a, **kw)
        op.create = flaky
        return S.Setup(dispatch=self.dispatch, op=op,
                       plan=lambda alias, system, user: self.plans.append(alias) or self.plan_text,
                       upsert_project=self.rows.append,
                       refresh=lambda chat_id, pid: self.refreshed.append((chat_id, pid)) or "",
                       today=lambda: date(2026, 10, 12))


def _state(**answers):
    st = N.fresh()
    st.update(stage="setup", id="tide-log", prd="# PRD: Tide Log\n")
    st["answers"].update({"name": "Tide Log", "problem": "Tides", "mvp": "A tide table page",
                          "stack": "Python, FastAPI", "visibility": "private",
                          "local_only": False, "autopilot": True, **answers})
    return st


def _run(world, st, chat_id="c1"):
    saves = []
    out = "".join(S.run(world.setup(), st, lambda s: saves.append(json.loads(json.dumps(s))),
                        chat_id))
    return out, saves


def test_the_steps_run_in_order_and_finish():
    w, st = World(), _state()
    out, saves = _run(w, st)
    routes = [c[0] for c in w.calls if c[0].startswith("/")]
    assert routes == ["/new-project", "/register-project", "/seed-board", "/project-stream"]
    repo = w.calls[0][1]
    assert repo["register"] is False and repo["prd"] == st["prd"] and repo["stack"] == ["python", "fastapi"]
    assert w.calls[1][1] == {"id": "tide-log", "local_only": False, "autopilot": True}
    for n in range(1, 8):
        assert f"✓ {n}/7" in out
    assert st["stage"] == "done" and st["steps"] == list(S.STEPS) and saves[-1]["stage"] == "done"
    assert w.rows == [{"title": "Tide Log", "status": "Planning", "owner": "Josias", "lane": "dev",
                       "stack": "python, fastapi", "repo": ""}]
    assert w.refreshed == [("c1", "tide-log")]
    assert "no GitHub remote" in out
    assert not any("push" in str(c[0]) or "github" in str(c).lower() for c in w.calls)


def test_the_roadmap_is_an_initiative_a_dated_feature_epics_and_tasks():
    w, st = World(), _state()
    _run(w, st)
    made = [c for c in w.calls if c[0] == "create"]
    assert [(c[1], c[2]) for c in made] == [
        ("nomad-portfolio", "Initiative"), ("tide-log", "Feature"), ("tide-log", "Epic"),
        ("tide-log", "Task"), ("tide-log", "Task"), ("tide-log", "Epic"), ("tide-log", "Task")]
    init, feat = st["roadmap"]["initiative"], st["roadmap"]["feature"]
    assert made[1][3] == "M1: A tide table page" and made[1][4] == init
    assert made[2][4] == feat and made[3][4] == st["roadmap"]["epic0"]
    assert all("/home/op/tide-log/docs/PRD.md" in c[5] for c in made)
    patch = next(c for c in w.calls if c[0] == "PATCH")
    assert patch[3] == {"lockVersion": 0, "startDate": "2026-10-12", "dueDate": "2026-11-02"}
    assert w.plans == ["balanced"]


def test_a_failed_step_resumes_there_and_runs_nothing_twice():
    w, st = World(fail={"/seed-board"}), _state()
    out, _ = _run(w, st)
    assert "⚠ 3/7 nomad-pm board failed: /seed-board: boom" in out
    assert st["stage"] == "setup" and st["steps"] == ["repo", "registry"]
    out, _ = _run(w, st)
    routes = [c[0] for c in w.calls if c[0].startswith("/")]
    assert routes == ["/new-project", "/register-project", "/seed-board", "/seed-board",
                      "/project-stream"]
    assert st["stage"] == "done"


def test_a_roadmap_cut_short_makes_no_twins():
    w, st = World(fail={"roadmap:4"}), _state()
    out, saves = _run(w, st)
    assert "4/7 Roadmap failed" in out and "OpenProject 503" in out
    assert saves[-1]["roadmap"]["epic0"]                    # saved before the failure
    _run(w, st)
    subjects = [c[3] for c in w.calls if c[0] == "create"]
    assert len(subjects) == len(set(subjects)) == 7
    assert sum(1 for c in w.calls if c[0] == "PATCH") == 1 and len(w.plans) == 1


def test_an_unusable_plan_gets_the_starter_plan():
    w, st = World(plan="Sure! Here's a plan: step one, step two."), _state()
    out, _ = _run(w, st)
    tasks = [c[3] for c in w.calls if c[0] == "create" and c[2] == "Task"]
    assert tasks[0] == "Set the test command in projects.yaml" and len(tasks) == 3
    assert "starter plan" in out and st["stage"] == "done"


def test_a_local_only_project_plans_on_private():
    w, st = World(), _state(local_only=True)
    _run(w, st)
    assert w.plans == ["private"] and w.calls[0][1]["local_only"] is True


def test_approve_prd_runs_setup_through_the_state_machine():
    w, st = World(), _state()
    st["stage"] = "prd"
    saves = []
    out = "".join(N.step("approve PRD", st, taken=set(), draft=lambda *a: "",
                         save=lambda s: saves.append(s["stage"]),
                         setup=lambda s, sv: S.run(w.setup(), s, sv, "c9")))
    assert "PRD approved" in out and "✓ 7/7" in out and st["stage"] == "done"
    assert w.refreshed == [("c9", "tide-log")]


def test_stack_of():
    assert S.stack_of("Python, FastAPI, runs on the host") == ["python", "fastapi", "runs-on-the-host"]
    assert S.stack_of("you pick") == []
    assert S.stack_of("Go and Postgres") == ["go", "postgres"]


def test_nocodb_find_encodes_a_title_with_spaces(monkeypatch):
    import nocodb
    db, seen = nocodb.NocoDB.__new__(nocodb.NocoDB), []
    monkeypatch.setattr(db, "_auth", lambda: None, raising=False)
    monkeypatch.setattr(db, "table_id", lambda t: "t1", raising=False)
    monkeypatch.setattr(db, "_req", lambda m, p, *a: seen.append(p) or {"list": []}, raising=False)
    db.find("projects", "title", "U6 Throwaway")
    assert seen == ["/api/v2/tables/t1/records?where=(title,eq,U6%20Throwaway)&limit=1"]
