"""#1151 · + New project from an Open WebUI chat: interview and PRD loop (owui_newproject.py,
docs/prd/owui-sessions.md §6.4 steps 1–2).

What they pin down:
  - the first message is the pitch; then one question per turn, re-asked when the answer
    doesn't fit (a taken or unusable name, a yes/no that isn't one)
  - the PRD is drafted after the last answer, by balanced, or by private for a private or
    local-only idea, and revised until `approve PRD`
  - nothing is set up before `approve PRD`: setup is called exactly once, after it
  - `cancel` clears the chat; a failed draft is retried on the next message
  - the chat turn saves the state, so a restarted engine picks up where it stopped

Run: python3 -m pytest v2/test_owui_newproject.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import owui_chat as O  # noqa: E402
import owui_newproject as N  # noqa: E402
from test_owui_chat import FakeLiteLLM, deps, run  # noqa: E402

ANSWERS = ["Tide Log", "Sailors lose track of tides", "Weekend sailors", "A tide table page",
           "Python, FastAPI, runs on the host", "public", "no", "yes"]


class Drafts:
    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def __call__(self, alias, system, user):
        self.calls.append((alias, system, user))
        if self.fail:
            self.fail -= 1
            raise TimeoutError("slow")
        return f"```markdown\n# PRD: Tide Log\n\n## Problem and why now\nv{len(self.calls)}\n```"


def talk(state, msgs, draft=None, taken=frozenset({"nomad", "civis-poc"}), setup=None):
    saves, out = [], []
    for m in msgs:
        out.append("".join(N.step(m, state, taken=taken, draft=draft or Drafts(),
                                  save=lambda st: saves.append(dict(st)), setup=setup)))
    return out, saves


def test_the_interview_asks_each_question_once_then_drafts():
    st, d = N.fresh(), Drafts()
    out, saves = talk(st, ["I want a tide log app", *ANSWERS], d)
    assert out[0].startswith("Let's set up a new project") and "**1/8.**" in out[0]
    for i in range(1, 8):
        assert f"**{i + 1}/8.**" in out[i]
    assert out[8].startswith("# PRD: Tide Log") and "`approve PRD`" in out[8]
    assert st["stage"] == "prd" and st["id"] == "tide-log"
    assert st["answers"]["visibility"] == "public" and st["answers"]["local_only"] is False
    assert st["answers"]["autopilot"] is True and st["answers"]["pitch"] == "I want a tide log app"
    assert len(d.calls) == 1 and d.calls[0][0] == "balanced"
    for section in N.SECTIONS:
        assert section in d.calls[0][1]
    assert "Weekend sailors" in d.calls[0][2] and saves[-1]["prd"] == st["prd"]


def test_bad_answers_are_asked_again():
    st = N.fresh()
    out, _ = talk(st, ["idea", "Nomad", "!!", "Tide Log", "p", "u", "m", "s", "v", "maybe"])
    assert "`nomad` is already a project" in out[1] and "**1/8.**" in out[1]
    assert "usable id" in out[2]
    assert "Please answer yes or no." in out[9] and "**7/8.**" in out[9]
    assert st["q"] == 6 and st["stage"] == "interview"
    out, _ = talk(N.fresh(), ["idea", "Tide Log"], taken=None)
    assert "can't check the name" in out[1]


def test_a_private_or_local_only_idea_is_drafted_locally():
    d = Drafts()
    talk(N.fresh(), ["keep it private: a diary", *ANSWERS], d)
    d2 = Drafts()
    talk(N.fresh(), ["a diary", *ANSWERS[:6], "yes", "no"], d2)
    assert d.calls[0][0] == d2.calls[0][0] == "private"


def test_nothing_is_set_up_before_approve_prd():
    st, d, ran = N.fresh(), Drafts(), []

    def setup(state, save):
        ran.append(state["id"])
        state["stage"] = "done"
        save(state)
        yield "all set"

    out, _ = talk(st, ["idea", *ANSWERS, "add a section on offline use", "approve", "looks good"],
                  d, setup=setup)
    assert ran == [] and st["stage"] == "prd" and len(d.calls) == 4
    assert "The operator's changes:\nadd a section on offline use" in d.calls[1][2]
    assert "The current PRD:" in d.calls[1][2]
    out, _ = talk(st, ["approve PRD"], d, setup=setup)
    assert ran == ["tide-log"] and "PRD approved" in out[0] and "all set" in out[0]
    out, _ = talk(st, ["approve PRD"], d, setup=setup)
    assert ran == ["tide-log"] and "Project dropdown" in out[0]


def test_without_setup_wired_the_approved_prd_waits():
    st = N.fresh()
    out, _ = talk(st, ["idea", *ANSWERS, "Approve the PRD", "hello"])
    assert st["stage"] == "setup" and N.NOT_WIRED in out[-2] and out[-1] == N.NOT_WIRED


def test_cancel_clears_and_a_failed_draft_retries():
    st = N.fresh()
    out, _ = talk(st, ["idea", "Tide Log", "cancel"])
    assert st == N.fresh() and out[-1].startswith("Cleared")
    st, d = N.fresh(), Drafts(fail=1)
    out, _ = talk(st, ["idea", *ANSWERS], d)
    assert "Drafting the PRD failed (TimeoutError" in out[-1] and st["prd"] == ""
    out, _ = talk(st, ["again"], d)
    assert out[0].startswith("# PRD") and st["stage"] == "prd"


def test_the_chat_turn_saves_state_and_resumes_after_a_restart():
    store, d = {}, Drafts()
    llm = FakeLiteLLM()

    def chat_deps():
        dd = deps(llm)
        dd.np_load = lambda c: store.get(c)
        dd.np_save = lambda c, st: store.__setitem__(c, dict(st))
        dd.draft = d
        return dd

    history = []
    for m in ["a tide log", *ANSWERS]:
        history.append(m)
        _, text = run({"entry": "new-project", "chat_id": "np1",
                       "messages": [{"role": "user", "content": m}]}, chat_deps())
    assert store["np1"]["stage"] == "prd" and "drafted by balanced" in text
    assert "# PRD: Tide Log" in text and llm.bodies == []
    events, text = run({"entry": "new-project", "chat_id": "np1",
                        "messages": [{"role": "user", "content": "approve PRD"}]}, chat_deps())
    assert store["np1"]["stage"] == "setup" and events[-1]["routed"] == "new-project"
    assert "new project · setup" in text


def test_a_new_project_chat_with_no_saved_state_store_creates_nothing():
    dd = deps(FakeLiteLLM())

    def boom(c):
        raise OSError("nocodb down")
    dd.np_load = boom
    _, text = run({"entry": "new-project", "chat_id": "np2",
                   "messages": [{"role": "user", "content": "hi"}]}, dd)
    assert "Can't continue this chat" in text and "Nothing was created" in text


TYPES = [{"id": "website", "title": "Website", "description": "A site.",
          "interview": {"ask": [{"id": "domain", "question": "Domain?", "kind": "text"}], "skip": []},
          "stack": {}, "policies": [], "agents": [], "gates": {}},
         {"id": "generic", "title": "Generic", "description": "Anything else.",
          "interview": {"ask": [], "skip": []}, "stack": {}, "policies": [], "agents": [], "gates": {}}]


def test_the_chat_asks_the_type_first_and_reads_the_types_only_while_it_needs_them():
    """#1373: deps.profiles is the dispatcher's GET /profiles; it is asked at the pitch and
    until a type is chosen, not on every turn after."""
    store, asked = {}, []

    def chat_deps():
        dd = deps(FakeLiteLLM())
        dd.np_load = lambda c: store.get(c)
        dd.np_save = lambda c, st: store.__setitem__(c, dict(st))
        dd.draft = Drafts()
        dd.profiles = lambda: asked.append(1) or TYPES
        return dd

    def say(m):
        return run({"entry": "new-project", "chat_id": "np9",
                    "messages": [{"role": "user", "content": m}]}, chat_deps())[1]

    text = say("a site for my bakery")
    assert "What kind of project is it?" in text and "**Website**" in text
    say("1")
    assert store["np9"]["type"] == "website" and len(asked) == 2
    say("Bakery Site")
    say("people want opening hours")
    assert len(asked) == 2


def test_the_chat_waits_when_the_types_cannot_be_read():
    store = {}

    def chat_deps(fail):
        dd = deps(FakeLiteLLM())
        dd.np_load = lambda c: store.get(c)
        dd.np_save = lambda c, st: store.__setitem__(c, dict(st))

        def profiles():
            if fail:
                raise OSError("dispatcher down")
            return TYPES
        dd.profiles = profiles
        return dd

    for m, fail in (("a site", True), ("website", True)):
        _, text = run({"entry": "new-project", "chat_id": "np8",
                       "messages": [{"role": "user", "content": m}]}, chat_deps(fail))
        assert N.NO_TYPES in text
    run({"entry": "new-project", "chat_id": "np8",
         "messages": [{"role": "user", "content": "website"}]}, chat_deps(False))
    assert store["np8"]["type"] == "website"
