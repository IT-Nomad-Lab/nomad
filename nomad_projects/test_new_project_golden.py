"""Golden test of today's + New project flow (#1371), before it moves to nomad_projects (#1372).

Fixed inputs go through the whole chat flow: v2/owui_newproject.step for the interview and the
PRD loop, then v2/owui_setup.run after `approve PRD`. Everything the flow sends out is recorded
and compared to testdata/new-project-golden.json:

  transcript    every reply, in order
  dispatch      each dispatcher call (route, body)
  registry      the projects.yaml entry /register-project appends (dispatch._registry_entry)
  board         the seed.rb BOARDS row /seed-board adds (dispatch._add_board)
  roadmap       each OpenProject create and the Feature's date PATCH
  console       the NocoDB `projects` row
  models        which alias drafted the PRD and the plan
  state         the chat state at the end

The fakes answer like the live services do; nothing touches the network or the disk outside
tmp. A change to any of these is a change to the flow: rerun with NOMAD_GOLDEN_WRITE=1 only when
the change is meant, and say so in the commit.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest nomad_projects/test_new_project_golden.py -q
"""
import importlib.util
import json
import os
import sys
from datetime import date

import pytest

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "new-project-golden.json")
sys.path.insert(0, os.path.join(NOMAD, "v2"))

import owui_newproject as N  # noqa: E402
import owui_setup as S  # noqa: E402


def _dispatch_module():
    sys.path.insert(0, os.path.join(NOMAD, "dispatcher"))
    spec = importlib.util.spec_from_file_location("nomad_dispatch_golden", os.path.join(NOMAD, "dispatcher", "dispatch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


D = _dispatch_module()
SEED_RB = 'BOARDS = [\n  ["nomad-work", "NOMAD work"], ["civis", "Civis"],\n  ["nomad-portfolio", "NOMAD Portfolio"]\n].freeze\n'
PRD = "# PRD: {name}\n\n## Problem and why now\nTides.\n"
PLAN = json.dumps({"m1": "A tide table page", "weeks": 3, "epics": [
    {"title": "Data", "tasks": ["Set the test command in projects.yaml", "Add the tide API client"]},
    {"title": "Page", "tasks": ["Render the table", "Show the next high tide"]}]})

SCENARIOS = {
    "cloud-autopilot": ["A log of tide times for my beach walks.", "Tide Log",
                        "I never know when low tide is.", "Me, then other walkers.", "A tide table page.",
                        "Python, FastAPI", "private", "no", "yes", "approve PRD"],
    "local-only-private": ["Keep this private: a journal for my health notes.", "Health Notes",
                           "I lose track of symptoms.", "Only me.", "A form and a list.",
                           "you pick", "private repo", "yes", "no", "Make the MVP smaller.", "approve PRD"],
}


class World:
    def __init__(self):
        self.dispatch_calls, self.roadmap, self.console, self.models = [], [], [], []
        self.registry, self.board, self.wps = [], [], {}

    def dispatch(self, route, body, timeout):
        self.dispatch_calls.append([route, body])
        if route == "/new-project":
            return {"ok": True, "path": f"/home/op/{body['id']}"}
        if route == "/register-project":
            self.registry.append(D._registry_entry(body["id"], body["id"], body["local_only"], body["autopilot"]))
            return {"ok": True, "merged": True, "branch": f"register/{body['id']}", "note": ""}
        if route == "/seed-board":
            self.board.append(D._add_board(SEED_RB, body["board"], body["name"]))
            return {"ok": True, "board": body["board"], "op_id": 77, "merged": True, "note": ""}
        if route == "/project-stream":
            return {"ok": True, "stream": body["id"]}
        raise AssertionError(f"unexpected route {route}")

    def op(self, board):
        world = self

        class Board:
            def create(self, type_name, subject, parent=None, lane="manager", description=""):
                wid = 900 + len(world.wps)
                world.wps[wid] = {"id": wid, "lockVersion": 0}
                world.roadmap.append(["create", board, type_name, subject, parent, lane, description])
                return world.wps[wid]

            def get(self, wid):
                return world.wps[wid]

            def call(self, method, path, body=None):
                world.roadmap.append([method, board, path, body])
                return {}
        return Board()

    def draft(self, alias, system, user):
        self.models.append(["prd", alias, system == N.PRD_SYSTEM])
        name = user.split("What's the project called?\n", 1)[1].split("\n", 1)[0]
        return PRD.format(name=name) + ("\n(revised)\n" if "The operator's changes" in user else "")

    def plan(self, alias, system, user):
        self.models.append(["plan", alias, system == S.PLAN_SYSTEM])
        return PLAN

    def setup(self):
        return S.Setup(dispatch=self.dispatch, op=self.op, plan=self.plan,
                       upsert_project=self.console.append,
                       refresh=lambda chat_id, pid: "", today=lambda: date(2026, 10, 12))


def run_scenario(msgs):
    w, state, saves = World(), N.fresh(), []
    setup = w.setup()
    transcript = []
    for m in msgs:
        out = "".join(N.step(m, state, taken=frozenset({"nomad", "civis-poc"}), draft=w.draft,
                             save=lambda s: saves.append(s.get("stage")),
                             setup=lambda st, save: S.run(setup, st, save, "chat-1")))
        transcript.append([m, out])
    return json.loads(json.dumps({
        "transcript": transcript, "dispatch": w.dispatch_calls, "registry": w.registry,
        "board": w.board, "roadmap": w.roadmap, "console": w.console, "models": w.models,
        "stages": [s for i, s in enumerate(saves) if i == 0 or saves[i - 1] != s], "state": state}))


def _golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def test_write_golden_when_asked():
    if not os.environ.get("NOMAD_GOLDEN_WRITE"):
        pytest.skip("set NOMAD_GOLDEN_WRITE=1 to rewrite the golden file")
    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, "w", encoding="utf-8") as f:
        json.dump({k: run_scenario(v) for k, v in SCENARIOS.items()}, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_flow_matches_the_golden_file(name):
    got, want = run_scenario(SCENARIOS[name]), _golden()[name]
    for key in sorted(set(got) | set(want)):           # one key at a time, so a diff names its part
        assert got.get(key) == want.get(key), key


def test_the_golden_file_covers_what_the_task_names():
    g = _golden()
    assert set(g) == set(SCENARIOS)
    cloud, local = g["cloud-autopilot"], g["local-only-private"]
    assert "autopilot:" in cloud["registry"][0] and "local_only: true" in local["registry"][0]
    assert all('["%s", ' % s["state"]["id"] in s["board"][0] for s in (cloud, local))
    assert [r[2] for r in cloud["roadmap"] if r[0] == "create"][:2] == ["Initiative", "Feature"]
    assert {m[1] for m in local["models"]} == {"private"} and {m[1] for m in cloud["models"]} == {"balanced"}
    assert cloud["state"]["stage"] == local["state"]["stage"] == "done"
