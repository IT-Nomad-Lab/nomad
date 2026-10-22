"""#1114 · GET /owui/models: the Open WebUI pipe's dropdown (docs/prd/owui-sessions.md §6.1).

Run: python3 -m pytest v2/test_owui_models.py -q   (engine deps live in the container)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import owui  # noqa: E402

TOKEN = "owui-test-token"
DETAIL = [
    {"name": "RuneMatch", "id": "runematch", "local_only": False},
    {"name": "Civis", "id": "civis-poc"},
    {"name": "Private Notes", "id": "private-notes", "local_only": True},
]


@pytest.fixture(autouse=True)
def no_token_files(monkeypatch):
    monkeypatch.setattr(owui, "TOKEN_FILES", ())
    monkeypatch.delenv("NOMAD_OWUI_TOKEN", raising=False)


def test_projects_by_name_then_new_project_then_general():
    rows = owui.models(DETAIL)["models"]
    assert [r["name"] for r in rows] == ["NOMAD · Civis", "NOMAD · Private Notes",
                                         "NOMAD · RuneMatch", "+ New project", "General"]
    assert [r["id"] for r in rows] == ["p-civis-poc", "p-private-notes", "p-runematch",
                                       "new-project", "general"]
    assert rows[0]["project"] == "civis-poc" and rows[3]["project"] is None


def test_local_only_projects_are_local_and_general_is_not():
    local = {r["id"]: r["local_only"] for r in owui.models(DETAIL)["models"]}
    assert local == {"p-civis-poc": False, "p-private-notes": True, "p-runematch": False,
                     "new-project": False, "general": False}


def test_dispatcher_down_still_offers_new_project_and_general():
    d = owui.models(None)
    assert [r["id"] for r in d["models"]] == ["new-project", "general"]
    assert d["projects_ok"] is False
    assert owui.models([])["projects_ok"] is True


def test_rows_without_an_id_are_dropped():
    assert [r["id"] for r in owui.models([{"name": "x"}])["models"]] == ["new-project", "general"]


def test_no_token_configured_closes_the_door():
    assert owui.check(TOKEN)[0] == 503
    assert owui.check("")[0] == 503


def test_token_from_env_or_file(monkeypatch, tmp_path):
    f = tmp_path / "owui-token"
    f.write_text(TOKEN + "\n")
    monkeypatch.setattr(owui, "TOKEN_FILES", (str(tmp_path / "missing"), str(f)))
    assert owui.check(TOKEN) == (0, "")
    assert owui.check("wrong")[0] == 401
    assert owui.check(None)[0] == 401
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", "from-env")
    assert owui.check("from-env") == (0, "") and owui.check(TOKEN)[0] == 401


# ── the route ──
fastapi = pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "AUTH_MODE", "off")
    monkeypatch.setattr(server, "_registry_detail", lambda: DETAIL)
    return TestClient(server.app)


def test_route_refuses_without_the_token(client, monkeypatch):
    assert client.get("/owui/models").status_code == 503          # none configured
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", TOKEN)
    assert client.get("/owui/models").status_code == 401
    assert client.get("/owui/models", headers={"X-Nomad-Token": "nope"}).status_code == 401


def test_route_lists_the_dropdown(client, monkeypatch):
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", TOKEN)
    r = client.get("/owui/models", headers={"X-Nomad-Token": TOKEN})
    assert r.status_code == 200
    d = r.json()
    assert d["projects_ok"] is True
    assert [m["id"] for m in d["models"]][-2:] == ["new-project", "general"]
    assert len(d["models"]) == 5


def test_route_survives_the_dispatcher_being_down(client, monkeypatch):
    monkeypatch.setenv("NOMAD_OWUI_TOKEN", TOKEN)
    monkeypatch.setattr(server, "_registry_detail", lambda: None)
    d = client.get("/owui/models", headers={"X-Nomad-Token": TOKEN}).json()
    assert d["projects_ok"] is False and [m["id"] for m in d["models"]] == ["new-project", "general"]
