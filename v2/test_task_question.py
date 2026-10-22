"""The Now queue's reading of a worker's question (#1175)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from task_question import parse_question  # noqa: E402


def test_two_numbered_options():
    q = parse_question("Which store should the art pack come from?\n1. Itch.io, 12 USD\n2. Unity store, 20 USD")
    assert q["stem"] == "Which store should the art pack come from?"
    assert [o["key"] for o in q["options"]] == ["1", "2"]
    assert q["options"][1]["text"] == "Unity store, 20 USD"
    assert q["complex"] is False


def test_option_words_and_markdown():
    q = parse_question("Pick one.\n**Option A**: ship now\n**Option B**: wait for review")
    assert [o["key"] for o in q["options"]] == ["A", "B"]
    assert q["options"][0]["text"] == "ship now"


def test_wrapped_option_lines_join():
    q = parse_question("Choose:\n1) first part\n   continues here\n2) second")
    assert q["options"][0]["text"] == "first part continues here"


def test_a_plain_question_has_no_options_and_is_shown_whole():
    q = parse_question("May I delete the old branch?")
    assert q == {"stem": "May I delete the old branch?", "options": [], "complex": False}


def test_a_long_comment_is_complex():
    q = parse_question("word " * 200)
    assert q["complex"] is True and q["options"] == []


def test_code_blocks_and_tables_are_complex():
    assert parse_question("Which?\n1. a\n2. b\n```\ncode\n```")["complex"] is True
    assert parse_question("| a | b |\n|---|---|\n| 1 | 2 |")["complex"] is True


def test_a_long_option_or_many_options_is_complex():
    assert parse_question("Which?\n1. " + "x" * 300 + "\n2. y")["complex"] is True
    many = "Which?\n" + "\n".join(f"{i}. opt" for i in range(1, 8))
    q = parse_question(many)
    assert q["complex"] is True and len(q["options"]) == 5


def test_a_single_numbered_line_is_not_a_choice():
    q = parse_question("Step 1. Do the thing")
    assert q["options"] == []
