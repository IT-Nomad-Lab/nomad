"""#776 · /run says which project a run belongs to, so the Zulip gate can.

The run already carried task_ref/task_pm and, for dev runs, the target repo, but /run passed none of
it on. run_project() reads the work package's title and board from OpenProject, fail-open and
cached, because the gate must post when the board is down and a cockpit with a detail open reads
/run every ten seconds.

Run: python3 -m pytest v2/test_run_project.py -q   (engine deps live in the container)
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402


class FakeOP:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def get(self, wp_id):
        self.calls.append(("get", wp_id))
        if self.fail:
            raise RuntimeError("OpenProject unreachable")
        return {"id": wp_id, "subject": "Fix the gate",
                "_links": {"project": {"href": "/api/v3/projects/7", "title": "NOMAD work"}}}

    def call(self, method, path, body=None, **k):
        self.calls.append((method, path))
        assert path == "/projects/7"
        return {"id": 7, "identifier": "nomad-work"}


@pytest.fixture
def op(monkeypatch):
    o = FakeOP()
    monkeypatch.setattr(type(server.eng), "op", property(lambda self: o))
    monkeypatch.setattr(server.pm_follow, "PUBLIC_URL", "https://pm.example")
    server._WP_FACTS.clear()
    yield o
    server._WP_FACTS.clear()


def test_an_openproject_task_names_its_board_title_and_link(op):
    p = server.run_project({"task_ref": "812", "task_pm": "openproject", "target": "nomad"}, "research")
    assert p == {"wp_id": 812, "wp_url": "https://pm.example/work_packages/812",
                 "wp_subject": "Fix the gate", "project_name": "NOMAD work", "board": "nomad-work"}


def test_a_dev_run_also_names_its_repo(op):
    p = server.run_project({"task_ref": 812, "task_pm": "openproject", "target": "nomad"}, "dev")
    assert p["repo"] == "nomad" and p["board"] == "nomad-work"
    assert server.run_project({"target": "civis"}, "dev") == {"repo": "civis"}


def test_a_run_with_no_project_is_none(op):
    assert server.run_project({"target": "operator"}, "comms") is None
    assert op.calls == []


def test_an_old_mission_control_task_keeps_its_id_and_asks_nobody(op):
    assert server.run_project({"task_ref": 44}, "comms") == {"task_ref": 44}
    assert op.calls == []


def test_the_board_being_down_still_gives_the_link(op):
    op.fail = True
    p = server.run_project({"task_ref": 9, "task_pm": "openproject"}, "comms")
    assert p["wp_id"] == 9 and p["wp_url"].endswith("/work_packages/9")
    assert "unreachable" in p["pm_error"] and "board" not in p


def test_openproject_is_asked_once_a_minute_not_every_read(op):
    for _ in range(5):
        server.run_project({"task_ref": 812, "task_pm": "openproject"}, "comms")
    assert op.calls == [("get", 812), ("GET", "/projects/7")]


def test_run_detail_carries_the_project(monkeypatch, op):
    class DB:
        def find(self, *a):
            return {"run_id": "r1", "lane": "dev", "status": "awaiting-approval",
                    "payload": json.dumps({"action": "dispatch_build", "target": "nomad",
                                           "task_ref": 812, "task_pm": "openproject", "args": {}})}

        def list(self, *a, **k):
            return []
    monkeypatch.setattr(server, "db", DB())
    monkeypatch.setattr(server, "_delivery", lambda rid: [])
    monkeypatch.setattr(server, "_artifacts", lambda rid: [])
    d = server.run_detail("r1")
    assert d["project"]["board"] == "nomad-work" and d["project"]["repo"] == "nomad"
