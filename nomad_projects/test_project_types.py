"""The type question in + New project (#1373, PRD docs/prd/project-types-and-inline-asks.md A3).

The interview runs through nomad_projects.create with the dispatcher's real GET /profiles
payload (profiles/*.yaml), and the registry entry is built the way /register-project builds it:
the type's gates read from its profile by the dispatcher, never sent by the caller. A3: the
same answers given to `website` and to `game` give different questions, gates and MCP grants,
from the profile alone.

Run: uv run --no-project --with pytest --with pyyaml --with requests python -m pytest nomad_projects/test_project_types.py -q
"""
import pytest

from nomad_policy import compiler, evaluator as ev
from nomad_projects import create as C
from nomad_projects import profiles
from nomad_projects.test_new_project_golden import D, World

PROFILES = D.profiles_payload()


class TypedWorld(World):
    def dispatch(self, route, body, timeout):
        if route != "/register-project":
            return super().dispatch(route, body, timeout)
        self.dispatch_calls.append([route, body])
        prof, pols, why = D._type_and_policies(body.get("type", ""), body.get("policies") or ())
        assert not why, why
        self.registry.append(D._registry_entry(body["id"], body["id"], body["local_only"],
                                               body["autopilot"], prof.id, pols, prof.gates))
        return {"ok": True, "merged": True, "branch": f"register/{body['id']}", "note": ""}


def run_type(type_answer, extra=None):
    """The full flow for one type, with the same default answers every time."""
    w, state = TypedWorld(), C.fresh()
    setup = w.setup()
    defaults = {"name": "Tide Log", "problem": "No idea when low tide is.", "users": "Me.",
                "mvp": "A tide table.", "stack": "you pick", "visibility": "private",
                "local_only": "no", "autopilot": "yes"}
    extra = extra or {}
    replies, asked = [], []

    def say(m):
        replies.append("".join(C.step(m, state, taken=frozenset({"nomad"}), draft=w.draft,
                                      save=lambda s: None, profiles=PROFILES["profiles"],
                                      setup=lambda st, save: C.run(setup, st, save, "chat-1"))))

    say("A thing for my weekend.")
    say(type_answer)
    while state["stage"] == "interview":
        key = C.questions(state)[state["q"]][0]
        asked.append(key)
        say(extra.get(key[2:], "you pick") if key.startswith("t.") else defaults[key])
    say("approve PRD")
    return w, state, replies, asked


def test_the_payload_lists_every_profile_generic_last():
    assert PROFILES["ok"], PROFILES
    ids = [p["id"] for p in PROFILES["profiles"]]
    assert sorted(ids) == sorted(profiles.load_all())
    assert ids[-1] == "generic"


def test_the_type_is_asked_first_with_every_type_and_other():
    state = C.fresh()
    reply = "".join(C.step("A site for my bakery.", state, taken=set(), draft=None,
                           save=lambda s: None, profiles=PROFILES["profiles"]))
    assert C.TYPE_Q in reply
    for p in PROFILES["profiles"]:
        assert p["title"] in reply
    assert "`other: <what it is>`" in reply
    assert C.questions(state)[0][0] == "type"


def test_website_and_game_differ_from_their_profile_alone():
    """A3: questions, the registry entry's gates, and the MCP grants differ."""
    ww, ws, _, wasked = run_type("website")
    gw, gs, _, gasked = run_type("game")
    assert ws["stage"] == gs["stage"] == "done"
    # questions
    assert [k for k in wasked if k.startswith("t.")] == ["t.domain", "t.framework", "t.design", "t.forms"]
    assert [k for k in gasked if k.startswith("t.")] == ["t.engine", "t.platform", "t.loop", "t.art"]
    # the registry entry: the type, and the website profile's gates in the autopilot block
    (went,), (gent,) = ww.registry, gw.registry
    assert "    type: website\n" in went and "    type: game\n" in gent
    assert 'pre_merge_gate: ["npm ci", "npm run build"]' in went
    assert "pre_merge_gate" not in gent
    # the caller never sends gates; the dispatcher reads them from the profile
    body = next(b for r, b in ww.dispatch_calls if r == "/register-project")
    assert set(body) == {"id", "local_only", "autopilot", "type", "policies"}
    # MCP grants and settings, from the type layer only
    pols = profiles.load_policies()
    lw = compiler.layers_for("website", pols)
    lg = compiler.layers_for("game", pols)
    assert ev.decide(lw, "mcp", "figma", {"channel": "gateway"}).allowed
    assert not ev.decide(lg, "mcp", "figma", {"channel": "gateway"}).allowed
    assert ev.values(lw)["cap"] == 2 and ev.values(lg)["cap"] == 1


def test_a_unity_game_gets_the_engine_policy_attached():
    w, state, replies, _ = run_type("game", {"engine": "1"})       # 1 = Unity
    assert state["answers"]["extra"]["engine"] == "Unity"
    body = next(b for r, b in w.dispatch_calls if r == "/register-project")
    assert body["policies"] == ["engine-unity"]
    assert "    policies: [engine-unity]\n" in w.registry[0]
    assert "engine policy" not in replies[-1]


def test_a_game_with_no_engine_yet_says_how_to_attach_it_later():
    w, state, replies, _ = run_type("game", {"engine": "not decided yet"})
    body = next(b for r, b in w.dispatch_calls if r == "/register-project")
    assert body["policies"] == []
    assert "policies: [engine-unity]" in replies[-1]
    assert "settle it in the PRD" in C._brief(state)


def test_other_maps_to_generic_and_flags_a_new_type():
    w, state, replies, asked = run_type("other: a hardware controller")
    assert state["type"] == "generic" and state["new_type"] == "a hardware controller"
    assert not [k for k in asked if k.startswith("t.")]
    assert "type:" not in w.registry[0]                 # generic is the default: not written
    assert "No project type fitted" in replies[-1]
    assert "a new project type may be needed" in C._brief(state)


@pytest.mark.parametrize("answer", ["2", "Website", "website"])
def test_a_type_is_picked_by_number_or_name(answer):
    plist = PROFILES["profiles"]
    if answer == "2":
        answer = str([p["id"] for p in plist].index("website") + 1)
    state = C.fresh()
    for m in ("pitch", answer):
        "".join(C.step(m, state, taken=set(), draft=None, save=lambda s: None, profiles=plist))
    assert state["type"] == "website" and "menu" not in state


def test_an_unknown_type_is_refused_and_asked_again():
    state = C.fresh()
    for m in ("pitch", "spaceship"):
        reply = "".join(C.step(m, state, taken=set(), draft=None, save=lambda s: None,
                               profiles=PROFILES["profiles"]))
    assert state["q"] == 0 and state["type"] == ""
    assert "Pick a number" in reply


def test_unreadable_types_wait_and_keep_the_pitch():
    state = C.fresh()
    reply = "".join(C.step("A site.", state, taken=set(), draft=None, save=lambda s: None, profiles=[]))
    assert C.NO_TYPES in reply
    reply = "".join(C.step("website", state, taken=set(), draft=None, save=lambda s: None, profiles=[]))
    assert C.NO_TYPES in reply and state["q"] == 0
    "".join(C.step("website", state, taken=set(), draft=None, save=lambda s: None,
                   profiles=PROFILES["profiles"]))
    assert state["type"] == "website" and state["answers"]["pitch"] == "A site."


def test_multi_select_takes_numbers_or_names():
    pq = {"id": "platform", "kind": "multi", "options": ["desktop", "mobile", "browser"]}
    assert C._choice(pq, "1, 3") == ["desktop", "browser"]
    assert C._choice(pq, "mobile and browser") == ["mobile", "browser"]
    assert C._choice(pq, "consoles") == ["consoles"]


def test_stack_left_open_takes_the_type_defaults():
    _, state, _, _ = run_type("website")
    assert C.stack_for(state) == ["typescript", "npm"]


def test_without_profiles_the_interview_is_as_before():
    state = C.fresh()
    reply = "".join(C.step("A thing.", state, taken=set(), draft=None, save=lambda s: None))
    assert "type" not in state and C.TYPE_Q not in reply
    assert C.questions(state)[0][0] == "name"
