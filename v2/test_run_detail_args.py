"""#779 · the run detail shows everything the approval covers.

The expanded row showed the first of body/brief/content/task/prompt and nothing else. For a
dispatch_build that is the one-line `task`, so builds were approved without the operator seeing
`plan`, the text the builder actually runs, or `branch`. arg_fields() now lays out every key of
`args`, whole, and the page draws that list.

Run: python3 -m pytest v2/test_run_detail_args.py -q   (engine deps live in the container)
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402

PLAN = "1. read the gate code\n2. pin the hash\n" + "step detail " * 150   # ~1,800 chars
BUILD = {"project": "nomad", "task": "pin the approval hash", "plan": PLAN,
         "branch": "fix/778", "run_id": "r-1"}


def keys(fields):
    return [f["key"] for f in fields]


def text(fields, key):
    [f] = [f for f in fields if f["key"] == key]
    return f


# ── arg_fields ───────────────────────────────────────────────────────────────────────────────

def test_a_build_shows_the_whole_plan_project_and_branch():
    fields = server.arg_fields(BUILD)
    assert text(fields, "plan")["text"] == PLAN
    assert text(fields, "plan")["long"] is True
    assert text(fields, "project")["text"] == "nomad"
    assert text(fields, "branch")["text"] == "fix/778"


def test_nothing_in_args_is_dropped():
    args = {**BUILD, "extra": {"nested": [1, 2]}, "flag": False, "count": 0, "blank": ""}
    assert sorted(keys(server.arg_fields(args))) == sorted(args)


def test_the_plan_comes_first_then_the_rest_in_stored_order():
    assert keys(server.arg_fields(BUILD)) == ["plan", "project", "task", "branch", "run_id"]


def test_a_mail_still_reads_subject_then_body():
    mail = {"to": "a@b.c", "subject": "Invoice", "body": "Hi,\nattached.", "run_id": "r-2"}
    fields = server.arg_fields(mail)
    assert keys(fields) == ["subject", "body", "to", "run_id"]
    assert text(fields, "body")["long"] is True and text(fields, "subject")["long"] is False


def test_research_and_content_keep_their_draft():
    assert text(server.arg_fields({"topic": "stt", "brief": "the brief"}), "brief")["text"] == "the brief"
    assert keys(server.arg_fields({"topic": "t", "content": "c"}))[0] == "content"


def test_non_text_values_are_shown_as_json_not_python_repr():
    f = text(server.arg_fields({"to": ["a@b.c", "d@e.f"], "flag": True}), "to")
    assert json.loads(f["text"]) == ["a@b.c", "d@e.f"]
    assert text(server.arg_fields({"flag": True}), "flag")["text"] == "true"


def test_a_long_one_liner_gets_a_wrapped_block():
    assert text(server.arg_fields({"prompt": "x" * 200}), "prompt")["long"] is True
    assert text(server.arg_fields({"prompt": "short"}), "prompt")["long"] is False


def test_args_that_are_not_a_dict_are_still_shown():
    assert server.arg_fields(["odd"]) == [{"key": "args", "text": '[\n  "odd"\n]', "long": True}]


# ── run_detail and the page ──────────────────────────────────────────────────────────────────

class FakeDB:
    def find(self, table, field, value):
        return {"run_id": "r-1", "lane": "dev", "status": "awaiting-approval",
                "payload": json.dumps({"action": "dispatch_build", "args": BUILD})}

    def list(self, *a, **k):
        return []


def test_run_detail_carries_the_fields(monkeypatch):
    monkeypatch.setattr(server, "db", FakeDB())
    monkeypatch.setattr(server, "_delivery", lambda rid: [])
    monkeypatch.setattr(server, "_artifacts", lambda rid: [])
    d = server.run_detail("r-1")
    assert d["arg_fields"] == server.arg_fields(BUILD)
    assert d["args"] == BUILD


def test_the_page_draws_every_field_and_no_longer_picks_one():
    m = re.search(r"async function loadDetail\(.*?\n}\n", server.COCKPIT, re.S)
    assert m
    body = m.group(0)
    assert "r.arg_fields" in body
    assert "a.body||a.brief||a.content||a.task||a.prompt" not in body
