"""The cockpit and run pages carry the NOMAD cockpit mark as the tab icon, and the files exist."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402

LINKS = ("/static/favicon.svg", "/static/favicon-96x96.png", "/static/apple-touch-icon.png")


def test_the_cockpit_head_links_the_mark():
    head = server.COCKPIT[:server.COCKPIT.index("</head>")]
    assert all(h in head for h in LINKS)


def test_the_files_are_served_from_the_static_dir():
    for h in LINKS:
        assert os.path.getsize(os.path.join(HERE, h.lstrip("/"))) > 100, h


def test_the_mark_keeps_the_family_style():
    svg = open(os.path.join(HERE, "static", "favicon.svg")).read()
    assert 'rx="128"' in svg and 'stroke="#ffffff"' in svg and 'stroke-linecap="round"' in svg
