"""#1116 · the local router for Open WebUI turns (docs/prd/owui-sessions.md §6.2 step 2).

Run: python3 -m pytest v2/test_owui_router.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import owui_router as R  # noqa: E402


class Router:
    """A fake `fast`: answers with `reply`, or raises it when it's an exception."""
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, alias, system, user, timeout):
        self.calls.append({"alias": alias, "user": user, "timeout": timeout})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def pick(model, **kw):
    return Router({"model": model, "reason": "because", "needs_tools": kw.pop("tools", False)})


def test_the_router_choice_is_used_and_asked_of_fast_with_a_short_timeout():
    r = pick("deep", tools=True)
    d = R.route("weigh splitting the engine", local_only=False, complete=r)
    assert (d.model, d.by, d.needs_tools, d.fallbacks) == ("deep", "router", True, None)
    assert r.calls[0]["alias"] == "fast" and r.calls[0]["timeout"] <= 8
    assert "Allowed models: fast, private, code, longdoc-local, balanced, deep, codex, longdoc" \
        in r.calls[0]["user"]


def test_local_only_offers_local_models_and_never_takes_a_cloud_answer():
    r = pick("deep")
    d = R.route("hello", local_only=True, complete=r)
    assert "Allowed models: fast, private, code, longdoc-local\n" in r.calls[0]["user"]
    assert (d.model, d.by) == ("private", "default") and "router skipped" in d.note
    assert d.fallbacks == ["fast"]


@pytest.mark.parametrize("reply", ["not json", "{\"model\": 3}", {"model": "gpt"},
                                   {"model": "nim"}, TimeoutError("slow"), OSError("down")])
def test_bad_or_failed_router_falls_back_to_the_default(reply):
    for local_only, want in ((False, "balanced"), (True, "private")):
        d = R.route("q", local_only=local_only, complete=Router(reply))
        assert (d.model, d.by) == (want, "default") and d.note.startswith("router skipped")


def test_an_image_goes_to_vision_without_asking():
    r = pick("fast")
    d = R.route("what is this?", local_only=False, complete=r, has_image=True)
    assert (d.model, d.by) == ("vision", "rule") and r.calls == []
    assert R.route("x", local_only=True, complete=r, has_image=True).fallbacks == ["vision-ollama"]


@pytest.mark.parametrize("cmd,want", [("/deep", "deep"), ("/local", "private"), ("/code", "code"),
                                      ("/codex", "codex"), ("/fast", "fast"),
                                      ("/longdoc-local", "longdoc-local"),
                                      ("!deep", "deep"), ("!local", "private"),
                                      ("!longdoc-local", "longdoc-local")])
def test_overrides_skip_the_router(cmd, want):
    r = pick("fast")
    d = R.route(f"{cmd} why is the sky blue", local_only=False, complete=r)
    assert (d.model, d.by, d.message) == (want, "override", "why is the sky blue") and r.calls == []


def test_an_override_alone_reasks_the_previous_question():
    hist = [{"role": "user", "content": "first q"}, {"role": "assistant", "content": "a"},
            {"role": "user", "content": "the question"}, {"role": "assistant", "content": "meh"}]
    d = R.route("/deep", local_only=False, complete=pick("fast"), history=hist)
    assert (d.model, d.message) == ("deep", "the question")


def test_a_bang_override_alone_reasks_and_skips_earlier_bare_overrides():
    """#1180: Open WebUI eats Enter on a lone `/deep`, so `!deep` is the form that gets through.
    A second override re-asks the question, not the first override."""
    hist = [{"role": "user", "content": "the question"}, {"role": "assistant", "content": "meh"},
            {"role": "user", "content": "!deep"}, {"role": "assistant", "content": "deeper"}]
    d = R.route("!local", local_only=False, complete=pick("fast"), history=hist)
    assert (d.model, d.by, d.reason, d.message) == ("private", "override", "!local", "the question")


def test_a_bang_cloud_override_is_refused_in_a_local_only_chat():
    d = R.route("!deep think hard", local_only=True, complete=pick("fast"))
    assert (d.model, d.message) == ("private", "think hard") and "!deep is a cloud model" in d.note


def test_a_cloud_override_is_refused_in_a_local_only_chat():
    d = R.route("/deep think hard", local_only=True, complete=pick("fast"))
    assert (d.model, d.message) == ("private", "think hard")
    assert "stays local" in d.note and d.fallbacks == ["fast"]


def test_an_unknown_bang_word_is_just_text():
    r = pick("fast")
    d = R.route("!important the build is red", local_only=False, complete=r)
    assert d.by == "router" and d.message == "!important the build is red"


def test_an_unknown_slash_word_is_just_text():
    r = pick("fast")
    d = R.route("/etc/hosts is wrong", local_only=False, complete=r)
    assert d.by == "router" and d.message == "/etc/hosts is wrong" and r.calls


def test_a_long_conversation_only_goes_to_models_that_hold_it():
    long = "word " * 9000                                  # ~11k tokens: past fast and private
    r = pick("fast")
    d = R.route(long, local_only=False, complete=r)
    assert "Allowed models: code, longdoc-local, balanced, deep, codex, longdoc" in r.calls[0]["user"]
    assert (d.model, d.by) == ("balanced", "default") and "doesn't fit" in d.note
    d = R.route(long, local_only=True, complete=pick("fast"))
    assert (d.model, d.by, d.fallbacks) == ("longdoc-local", "rule", [])
    huge = "word " * 170000                                # past 200k: only longdoc
    assert R.route(huge, local_only=False, complete=pick("fast")).model == "longdoc"


def test_local_fallbacks_never_leave_the_machine_and_nothing_names_gpt():
    for alias, chain in R.LOCAL_FALLBACKS.items():
        assert alias in R.LOCAL
        assert all(c not in R.CLOUD + R.PAID for c in chain)
    assert "gpt" not in R.LOCAL + R.CLOUD
    assert "gpt" not in R.OVERRIDES and "gpt" not in R.OVERRIDES.values()


def test_context_budget_scales_with_the_window():
    assert R.context_budget("fast") == 2867
    assert R.context_budget("balanced") == 8000


def test_project_context_takes_its_budget_out_of_the_window():
    mid = "word " * 4000                                   # ~5k tokens: fits fast alone, not with context
    r = pick("fast")
    R.route(mid, local_only=False, complete=r)
    assert "Allowed models: fast, private," in r.calls[0]["user"]
    r = pick("fast")
    R.route(mid, local_only=False, complete=r, context_tokens=1)
    assert "Allowed models: code, longdoc-local, balanced" in r.calls[0]["user"]


def test_override_helper_matches_route():
    d = R.override("/deep why?", local_only=False)
    assert (d.model, d.message, d.by) == ("deep", "why?", "override")
    assert R.override("hello", local_only=False) is None
    assert R.override("!foo bar", local_only=False) is None
    hist = [{"role": "user", "content": "q1"}, {"role": "user", "content": "!local"}]
    assert R.override("!deep", local_only=False, history=hist).message == "q1"
    d = R.override("!codex x", local_only=True)
    assert d.model == "private" and d.note == "!codex is a cloud model; this chat stays local"
