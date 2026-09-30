"""V2c · what the intent router matches, and more importantly what it refuses to match.

`classify` decides whether a sentence is a command or conversation, and the expensive direction of
error is only one of them. A command read as chat costs a repeated sentence. Chat read as a command
fires the gate: "I approve of that approach" resolving a pending run is an approval nobody gave.

These are the classifier only. It is pure and has no engine behind it, so it runs on the host as
well as in the container, which is the whole reason it is a separate module from the handlers.

Run: python3 -m pytest v2/test_intents.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import intents  # noqa: E402


def c(t):
    return intents.classify(t)[0]


def p(t):
    return intents.classify(t)[1]


# ── the refusals, first, because they are the ones that cost something ──────────────────────────

@pytest.mark.parametrize("text", [
    "I approve of that approach",
    "she approved the budget last week",
    "do you think the team will approve this?",
    "that is the approved way to do it",
    "my manager rejected the proposal, what now",
])
def test_approval_words_mid_sentence_are_not_a_decision(text):
    """The anchor at the start of the pattern is load-bearing. A chat turn that merely mentions
    approving must not resolve a pending run, because the operator never decided anything."""
    assert c(text) == "chat"


def test_a_capture_that_says_approve_is_still_a_capture():
    """`/capture` is checked before the gate patterns on purpose: the words inside a goal are the
    goal's words, not a decision about the goal."""
    i, pay = intents.classify("/capture approve the Q4 budget with finance")
    assert i == "pipeline_capture"
    assert pay["goal"] == "approve the Q4 budget with finance"


def test_an_empty_message_is_chat_not_a_command():
    assert c("") == "chat"
    assert c("   ") == "chat"
    assert c(None) == "chat"


def test_a_question_about_sending_is_not_a_send():
    """`_ACTION` is anchored too. Asking about a send is not ordering one."""
    assert c("did you send that email yet?") == "chat"
    assert c("what would you send to a new lead?") == "chat"


# ── gate decisions ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "approve", "approved", "Approve.", "ok, approve", "yes, approve it",
    "go ahead and approve", "please approve", "confirm", "permission granted", "authorize",
])
def test_an_approval_at_the_start_is_a_decision(text):
    i, pay = intents.classify(text)
    assert (i, pay["decision"]) == ("gate_decision", "approved")


@pytest.mark.parametrize("text", [
    "reject", "rejected", "no, reject it", "deny", "decline", "declined", "disapprove",
    "don't approve", "do not approve",
])
def test_a_rejection_at_the_start_is_a_decision(text):
    i, pay = intents.classify(text)
    assert (i, pay["decision"]) == ("gate_decision", "rejected")


def test_all_is_carried_through_so_the_handler_can_tell_one_from_many():
    """The word is what separates "approve the one I am looking at" from "approve everything
    queued", and the handler refuses to guess without it."""
    assert p("approve all")["all"] is True
    assert p("approve")["all"] is False
    assert p("reject all of them")["all"] is True


# ── diagnostics ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "run diagnostics", "diagnostics", "sitrep", "status report", "health check",
    "how are the systems", "how are things", "system status", "run a full self-test",
    "all systems", "give me the status of the system",
])
def test_diagnostic_phrasings(text):
    assert c(text) == "diagnostics"


def test_diagnostics_matches_anywhere_because_the_phrasing_varies():
    """Unlike the gate patterns this one searches rather than anchors, and that is the right
    trade: a diagnostic fired by mistake reads a few gauges and nothing else happens."""
    assert c("before we start, run diagnostics please") == "diagnostics"


# ── self development ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "work on your backlog", "work on the backlog", "develop yourself", "improve yourself",
    "self-develop", "process your backlog", "start the self development",
])
def test_self_development_phrasings(text):
    assert c(text) == "self_develop"


# ── starting a project ──────────────────────────────────────────────────────────────────────────

def test_a_project_name_is_pulled_out_of_the_sentence():
    i, pay = intents.classify("start a new project called Aurora")
    assert i == "start_project"
    assert pay["name"] == "Aurora"


def test_the_whole_sentence_is_kept_as_the_request():
    """The name scaffolds the repo; the sentence is what gets planned into milestones. Dropping it
    would leave the project on the board as a bare name with nothing to decompose."""
    pay = p("spin up a project called Aurora for tracking solar output")
    assert pay["request"] == "spin up a project called Aurora for tracking solar output"


@pytest.mark.parametrize("text,name", [
    ("create a project named Helios", "Helios"),
    ("kick off a new project titled Deep Field", "Deep Field"),
    ("scaffold a project for Ledger", "Ledger"),
])
def test_project_naming_forms(text, name):
    assert p(text)["name"] == name


def test_articles_are_stripped_out_of_a_name():
    """`_clean_name` exists because "a new project" would otherwise scaffold a repo called `new`."""
    assert intents._clean_name("the new Aurora") == "Aurora"


def test_a_project_with_no_name_still_classifies():
    """Better to reach the handler and be asked for a name than to fall through to chat, which
    would answer with enthusiasm and create nothing."""
    i, pay = intents.classify("start a new project")
    assert i == "start_project"
    assert pay["name"] in ("", "project")


# ── research ────────────────────────────────────────────────────────────────────────────────────

def test_research_carries_its_topic():
    i, pay = intents.classify("research the best local STT engine")
    assert i == "research"
    assert pay["topic"] == "the best local STT engine"


@pytest.mark.parametrize("text", [
    "look into WSL2 networking", "investigate the n8n gate", "dig into Qdrant sharding",
    "brief me on OpenProject", "search the web for Piper voices", "google local LLM quantisation",
])
def test_research_phrasings(text):
    assert c(text) == "research"


def test_a_research_verb_with_nothing_after_it_is_not_research():
    """A three-character floor. "research" alone is a topic nobody named, and capturing it would
    file a run whose goal is the word research."""
    assert c("research") == "chat"
    assert c("look into it") in ("chat", "research")


# ── imperative actions ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "send an email to the client", "publish the post", "share the deck with finance",
    "schedule a call for Tuesday", "draft a reply to that thread",
    "generate an image for the launch banner", "write the ad copy for the campaign",
])
def test_imperative_external_actions_capture(text):
    """They capture rather than execute. That is the point of routing them here at all: a chat
    surface that can act without the gate is the gate not existing."""
    assert c(text) == "action_capture"


def test_the_action_request_is_the_sentence_itself():
    assert p("send an email to the client")["request"] == "send an email to the client"


# ── the contract with the server ────────────────────────────────────────────────────────────────

def test_every_intent_the_classifier_can_emit_is_declared():
    """`INTENTS` is what the server checks its handler table against. If the classifier grows an
    intent and this tuple does not, the new intent falls through to chat, which looks exactly like
    normal behaviour and is the one failure here nobody would notice."""
    samples = ["/capture x", "approve", "run diagnostics", "work on your backlog",
               "start a project called X", "research local STT", "send an email", "hello"]
    for s in samples:
        assert c(s) in intents.INTENTS


def test_the_declared_intents_have_no_duplicates():
    assert len(intents.INTENTS) == len(set(intents.INTENTS))
