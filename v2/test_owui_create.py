"""The engine's POST /owui/create-project (#1374): nomad_projects.create from answers given at once,
behind the Open WebUI token, saved per step under new:<id>, resumed by the same call.

The setup steps are faked here: test_create_entry_points.py shows they are the chat's own.
"""
import time

import pytest

fastapi = pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PROFILES = [{"id": "website", "title": "Website", "description": "A site."},
            {"id": "generic", "title": "Generic", "description": "Anything else."}]
INPUTS = {"name": "Tide Log", "prd": "# PRD: Tide Log\n", "type": "website",
          "problem": "Tides.", "local_only": False, "autopilot": False}


class Store:
    def __init__(self):
        self.rows = []

    def create_table(self, name, cols):
        pass

    def find(self, table, field, value):
        return next((r for r in self.rows if r[field] == value), None)

    def create(self, table, fields):
        self.rows.append({"Id": len(self.rows) + 1, **fields})

    def update(self, table, rid, fields):
        self.rows[rid - 1].update(fields)


@pytest.fixture
def engine(monkeypatch):
    calls = {"setup": [], "fail": [False]}

    def setup(state, save, chat_id):
        calls["setup"].append((list(state.get("steps") or []), chat_id))
        done = state.setdefault("steps", [])
        if calls["fail"][0] and not done:
            done.append("repo")
            save(state)
            yield "⚠ 2/7 Registry failed: no answer"
            return
        done.extend(s for s in ("repo", "registry", "board") if s not in done)
        state["stage"] = "done"
        save(state)
        yield "✓ set up"

    monkeypatch.setattr(server, "AUTH_MODE", "off")
    monkeypatch.setattr(server, "db", Store())
    monkeypatch.setattr(server, "_owui_np", {"ok": False})
    monkeypatch.setattr(server, "_creating", {})
    monkeypatch.setattr(server, "_registry_detail", lambda: [{"id": "nomad"}])
    monkeypatch.setattr(server, "_owui_profiles", lambda: PROFILES)
    monkeypatch.setattr(server, "_owui_setup", setup)
    monkeypatch.setattr(server.owui, "TOKEN_FILES", ())
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", "t")
    return TestClient(server.app), calls


H = {"X-Nomad-Token": "t"}


def _wait(client, pid):
    for _ in range(100):
        got = client.get(f"/owui/create-project/{pid}", headers=H).json()
        if not got["running"]:
            return got
        time.sleep(0.02)
    raise AssertionError("the run never stopped")


def test_the_route_is_behind_the_token(engine):
    client, _ = engine
    assert client.post("/owui/create-project", json=INPUTS).status_code == 401
    assert client.get("/owui/create-project/tide-log").status_code == 401


def test_a_refused_answer_comes_back_at_once_and_makes_nothing(engine):
    client, calls = engine
    r = client.post("/owui/create-project", json={**INPUTS, "name": "Nomad"}, headers=H)
    assert r.status_code == 400 and "already a project" in r.json()["error"]
    r = client.post("/owui/create-project", json={**INPUTS, "prd": ""}, headers=H)
    assert r.status_code == 400 and "PRD" in r.json()["error"]
    assert client.post("/owui/create-project", json={"name": "!"}, headers=H).status_code == 400
    assert calls["setup"] == [] and server.db.rows == []
    assert client.get("/owui/create-project/tide-log", headers=H).status_code == 404


def test_it_runs_the_steps_in_a_thread_and_saves_under_new(engine):
    client, calls = engine
    r = client.post("/owui/create-project", json=INPUTS, headers=H)
    assert r.status_code == 202 and r.json()["id"] == "tide-log"
    got = _wait(client, "tide-log")
    assert got["stage"] == "done" and got["lines"][-1] == "✓ set up"
    row = server.db.find("owui_new_projects", "chat_id", "new:tide-log")
    assert row["project"] == "tide-log" and row["stage"] == "done"
    assert calls["setup"] == [([], "")]                    # no chat to bind
    state = server._owui_np_load("new:tide-log")
    assert state["type"] == "website" and state["prd"] == "# PRD: Tide Log"
    r = client.post("/owui/create-project", json=INPUTS, headers=H)
    assert r.status_code == 200 and r.json()["stage"] == "done" and len(calls["setup"]) == 1


def test_the_same_call_again_resumes_after_a_failed_step(engine, monkeypatch):
    client, calls = engine
    calls["fail"][0] = True
    client.post("/owui/create-project", json=INPUTS, headers=H)
    got = _wait(client, "tide-log")
    assert got["stage"] == "setup" and got["steps"] == ["repo"] and "failed" in got["lines"][-1]
    calls["fail"][0] = False
    # The id is in the registry by now; a resumed run does not ask the interview again.
    monkeypatch.setattr(server, "_registry_detail", lambda: [{"id": "nomad"}, {"id": "tide-log"}])
    assert client.post("/owui/create-project", json=INPUTS, headers=H).status_code == 202
    assert _wait(client, "tide-log")["stage"] == "done"
    assert calls["setup"][-1] == (["repo"], "")


def test_unreadable_types_refuse(engine, monkeypatch):
    client, _ = engine
    monkeypatch.setattr(server, "_owui_profiles", lambda: [])
    r = client.post("/owui/create-project", json=INPUTS, headers=H)
    assert r.status_code == 400 and "can't be read" in r.json()["error"]
