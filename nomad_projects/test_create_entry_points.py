"""One creation process for every entry point (#1374, PRD docs/prd/project-types-and-inline-asks.md A4).

The Open WebUI chat answers one question per turn (`step`); the engine's POST /owui/create-project,
the dispatcher's POST /create-project and the `create_project` MCP tool all end in `create`, with
the answers at once. A4: for the same answers, both make the same dispatcher calls, registry
entry, board, roadmap and mission-control row, and leave the same state. Re-running after a
failed step resumes at that step and makes no twins.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest nomad_projects/test_create_entry_points.py -q
"""
import pytest

from nomad_projects import create as C
from nomad_projects.test_new_project_golden import SCENARIOS, World, run_scenario
from nomad_projects.test_project_types import PROFILES, TypedWorld, run_type

TAKEN = frozenset({"nomad", "civis-poc"})
SAME = ("dispatch", "registry", "board", "roadmap", "console")

# SCENARIOS' chat answers, given at once.
INPUTS = {
    "cloud-autopilot": {
        "pitch": "A log of tide times for my beach walks.", "name": "Tide Log",
        "problem": "I never know when low tide is.", "users": "Me, then other walkers.",
        "mvp": "A tide table page.", "stack": ["Python", "FastAPI"], "visibility": "private",
        "local_only": False, "autopilot": True},
    "local-only-private": {
        "pitch": "Keep this private: a journal for my health notes.", "name": "Health Notes",
        "problem": "I lose track of symptoms.", "users": "Only me.", "mvp": "A form and a list.",
        "visibility": "private repo", "local_only": True, "autopilot": False},
}


def run_create(w, inputs, state=None, profiles=None, taken=TAKEN):
    state = {} if state is None else state
    setup = w.setup()
    out = "".join(C.create(inputs, state, taken=taken, save=lambda s: None, profiles=profiles,
                           setup=lambda st, save: C.run(setup, st, save, "")))
    return state, out


def _plans(models):
    return [m for m in models if m[0] == "plan"]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_create_matches_the_chat(name):
    chat = run_scenario(SCENARIOS[name])
    w = World()
    state, out = run_create(w, {**INPUTS[name], "prd": chat["state"]["prd"]})
    assert "✓ 7/7" in out, out
    got = {"dispatch": w.dispatch_calls, "registry": w.registry, "board": w.board,
           "roadmap": w.roadmap, "console": w.console}
    for key in SAME:
        assert got[key] == chat[key], key
    assert _plans(w.models) == _plans(chat["models"])
    assert state == chat["state"]


def test_a_typed_game_matches_the_chat():
    cw, cstate, _, _ = run_type("game", extra={"engine": "Unity"})
    w = TypedWorld()
    inputs = {"pitch": "A thing for my weekend.", "name": "Tide Log", "type": "game",
              "problem": "No idea when low tide is.", "users": "Me.", "mvp": "A tide table.",
              "local_only": False, "autopilot": True, "extra": {"engine": "Unity"},
              "prd": cstate["prd"]}
    state, out = run_create(w, inputs, profiles=PROFILES["profiles"], taken=frozenset({"nomad"}))
    assert "as type `game`" in out and "engine-unity attached" in out, out
    for key in ("dispatch_calls", "registry", "board", "roadmap", "console"):
        assert getattr(w, key) == getattr(cw, key), key
    assert state == cstate


def test_other_becomes_generic_and_is_flagged():
    w = TypedWorld()
    state, out = run_create(w, {"name": "Kiln Log", "new_type": "a kiln controller",
                                "prd": "# PRD: Kiln Log\n"}, profiles=PROFILES["profiles"])
    assert state["type"] == "generic" and state["new_type"] == "a kiln controller"
    assert "No project type fitted" in out


class FlakyBoard(World):
    """/seed-board fails the first time, as when OpenProject is down."""
    def __init__(self):
        super().__init__()
        self.failed = False

    def dispatch(self, route, body, timeout):
        if route == "/seed-board" and not self.failed:
            self.failed = True
            self.dispatch_calls.append([route, body])
            return {"ok": False, "error": "OpenProject is not answering"}
        return super().dispatch(route, body, timeout)


def test_a_rerun_after_a_failure_resumes_at_that_step():
    w, inputs = FlakyBoard(), {**INPUTS["cloud-autopilot"], "prd": "# PRD: Tide Log\n"}
    state, out = run_create(w, inputs)
    assert "3/7 nomad-pm board failed" in out and state["steps"] == ["repo", "registry"]
    state, out = run_create(w, inputs, state)      # the same call again: the id is taken now
    assert "✓ 7/7" in out and state["stage"] == "done"
    routes = [c[0] for c in w.dispatch_calls]
    assert routes.count("/new-project") == routes.count("/register-project") == 1
    assert routes.count("/seed-board") == 2
    assert len([r for r in w.roadmap if r[0] == "create"]) == 1 + 1 + 2 + 4   # no twins
    _, again = run_create(w, inputs, state)
    assert "already set up" in again


@pytest.mark.parametrize("inputs,profiles,why", [
    ({"name": "Nomad", "prd": "# PRD"}, None, "`nomad` is already a project"),
    ({"name": "!!", "prd": "# PRD"}, None, "usable id"),
    ({"name": "Tide Log"}, None, "PRD the operator approved"),
    ({"prd": "# PRD"}, None, "needs a name"),
    ({"name": "Tide Log", "prd": "# PRD", "type": "spaceship"}, PROFILES["profiles"], "Pick a number"),
    ({"name": "Tide Log", "prd": "# PRD", "type": "game"}, [], "can't be read"),
])
def test_a_refused_answer_creates_nothing(inputs, profiles, why):
    w = World()
    state, out = run_create(w, inputs, profiles=profiles)
    assert out.startswith("⚠ Not created") and why in out, out
    assert state == {} and w.dispatch_calls == []


def test_a_saved_run_refuses_another_project():
    w = World()
    state, _ = run_create(w, {**INPUTS["cloud-autopilot"], "prd": "# PRD"})
    _, out = run_create(w, {"name": "Health Notes", "prd": "# PRD"}, state)
    assert "not `health-notes`" in out
