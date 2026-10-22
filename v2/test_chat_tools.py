"""#968 · the cockpit chat's read-only tools (v2/chat_tools.py).

Three claims. Each tool answers from the same source the cockpit reads, so the chat and the page
agree. Each tool only reads: the NocoDB client's writes raise here, and every supervisor call is
recorded and has to be a GET. And `chat_reply` reaches them only through its allowlist (#970).

The NocoDB tables and the supervisor are fakes with known rows, so every answer is checked
against data the test wrote.

Run: python3 -m pytest v2/test_chat_tools.py -q   (engine deps live in the container)
"""
import ast
import inspect
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402
import chat_tools  # noqa: E402

FAILED = "25693e0adecf"
GATED = "f7afa3734f63"
GATED_TWIN = "f7afa3990000"     # shares a 6-char prefix with GATED
DONE = "0a1b2c3d4e5f"

COMMS = [  # newest first, as `sort="-Id"` returns them
    {"Id": 4, "run_id": GATED_TWIN, "lane": "comms", "status": "executed",
     "created_at": "2026-10-01T09:00:00+00:00", "payload": "{}"},
    {"Id": 3, "run_id": GATED, "lane": "comms", "status": "awaiting-approval",
     "created_at": "2026-10-01T08:00:00+00:00",
     "payload": json.dumps({"action": "send_message", "goal": "mail the brief",
                            "args": {"to": "josias@example.com", "body": "Here is the brief."}})},
    {"Id": 2, "run_id": FAILED, "lane": "research", "status": "failed",
     "created_at": "2026-09-30T08:00:00+00:00",
     "payload": json.dumps({"goal": "research X", "error": "SMTP 535 auth failed"})},
    {"Id": 1, "run_id": DONE, "lane": "dev", "status": "executed",
     "created_at": "2026-09-29T08:00:00+00:00", "payload": "{}"},
]
EPISODIC = [{"run_id": FAILED, "agent": "research", "what": f"step {i}",
             "created_at": f"2026-09-30T08:{i:02d}:00"} for i in range(50)]

SUP_STATUS = {"ok": True, "config": {"budget_mode": "cap", "daily_cap": 20},
              "spend": {"today": 3.5},
              "projects": {"runematch": {"enabled": True, "state": "running"},
                           "nomad": {"enabled": True, "state": "needs you"}}}
SUP_CONFIG = {"ok": True, "config": {"budget_mode": "cap", "daily_cap": 20}}
SUP_RUNS = [
    {"project": "nomad", "task": "968", "branch": "feat/968", "started": 2.0, "finished": None,
     "outcome": "running", "result": ""},
    {"project": "runematch", "task": "648", "branch": "feat/648", "started": 1.0,
     "finished": 1.5, "outcome": "ready to merge", "result": "autopilot: result ready to merge"},
]


class Refused(AssertionError):
    pass


@pytest.fixture
def board(monkeypatch):
    """Fake NocoDB with known rows, and a fake supervisor that records every call. Every NocoDB
    write raises, so a tool that writes fails the test that called it."""
    writes = []

    def fake_list(table, limit=50, where=None, sort=None):
        if table == "comms":
            return COMMS[:limit]
        if table == "episodic":
            return EPISODIC
        return []

    def fake_find(table, field, value):
        return next((r for r in COMMS if r.get(field) == value), None) if table == "comms" else None

    def refuse(name):
        def f(*a, **k):
            writes.append(name)
            raise Refused(f"db.{name} called by a read-only tool")
        return f

    monkeypatch.setattr(server.db, "list", fake_list, raising=False)
    monkeypatch.setattr(server.db, "find", fake_find, raising=False)
    for name in ("create", "update", "delete", "create_table", "_req"):
        monkeypatch.setattr(server.db, name, refuse(name), raising=False)

    calls = []

    def fake_autopilot(method, path, body=None):
        calls.append((method, path, body))
        if method != "GET":
            raise Refused(f"supervisor {method} {path}")
        if path == "/status":
            return 200, SUP_STATUS
        if path == "/config":
            return 200, SUP_CONFIG
        if path.startswith("/runs"):
            n = int(path.split("limit=")[1])
            return 200, {"ok": True, "runs": SUP_RUNS[:n]}
        return 404, {"ok": False, "error": "not found"}

    monkeypatch.setattr(server, "_autopilot", fake_autopilot)
    return {"writes": writes, "calls": calls}


# ── the entries ──────────────────────────────────────────────────────────────────────────────

EXPECTED = {"pipeline_runs", "pipeline_run_detail", "approval_queue", "autopilot_runs",
            "autopilot_status", "autopilot_config", "system_health"}


def test_the_tools_are_the_prd_list_each_its_own_entry():
    """PRD 4.1, by name. #970 builds its allowlist out of these entries."""
    assert set(chat_tools.TOOLS) == EXPECTED
    for name, t in chat_tools.TOOLS.items():
        assert t["name"] == name and t["read_only"] is True
        assert callable(t["handler"]) and t["description"]
        assert t["input_schema"]["type"] == "object"
        assert t["input_schema"].get("additionalProperties") is False


def test_chat_reply_reaches_them_only_through_the_allowlist():
    """#970 wired them in. Every one of the seven is named in the allowlist, and what the chat
    hands the model is exactly the allowlisted entries."""
    assert set(chat_tools.TOOLS) <= set(server.CHAT_TOOL_ALLOWLIST)
    assert set(chat_tools.TOOLS) <= set(server._chat_tools())


def test_an_unknown_tool_or_argument_is_an_error_not_a_crash(board):
    assert "error" in chat_tools.call("approve_run", {"run_id": GATED})
    assert "error" in chat_tools.call("pipeline_runs", {"limit": 5, "delete": True})
    assert "error" in chat_tools.call("pipeline_runs", ["nope"])


def test_a_failing_read_comes_back_as_text(board, monkeypatch):
    """PRD R6: the model says the read failed, it doesn't guess."""
    monkeypatch.setattr(server, "recent_runs",
                        lambda limit=25: (_ for _ in ()).throw(RuntimeError("nocodb down")))
    out = chat_tools.call("pipeline_runs")
    assert "nocodb down" in out["error"]


# ── pipeline_runs ────────────────────────────────────────────────────────────────────────────

def test_pipeline_runs_matches_what_the_cockpit_lists(board):
    out = chat_tools.call("pipeline_runs", {"limit": 10})
    cockpit = server.recent_runs(10)                 # what GET /runs serves
    assert [r["run_id"] for r in out["runs"]] == [r["run_id"] for r in cockpit]
    assert [r["status"] for r in out["runs"]] == [r["status"] for r in cockpit]
    assert out["count"] == 4
    gated = next(r for r in out["runs"] if r["run_id"] == GATED)
    assert gated["proposal"] == {"action": "send_message", "target": "josias@example.com"}


def test_pipeline_runs_filters_by_status_and_caps_the_limit(board):
    out = chat_tools.call("pipeline_runs", {"status": "failed"})
    assert [r["run_id"] for r in out["runs"]] == [FAILED]
    assert chat_tools.call("pipeline_runs", {"limit": 1})["count"] == 1
    assert chat_tools.call("pipeline_runs", {"limit": 999})["count"] == 4   # capped at 50


# ── pipeline_run_detail ──────────────────────────────────────────────────────────────────────

def test_run_detail_carries_the_real_error(board):
    out = chat_tools.call("pipeline_run_detail", {"run_id": FAILED})
    assert out["found"] is True and out["status"] == "failed"
    assert out["error"] == "SMTP 535 auth failed" and out["goal"] == "research X"
    assert out["run_id"] == server.run_detail(FAILED)["run_id"]


def test_run_detail_resolves_a_unique_prefix(board):
    out = chat_tools.call("pipeline_run_detail", {"run_id": "25693E"})
    assert out["found"] is True and out["run_id"] == FAILED


def test_an_ambiguous_prefix_returns_the_candidates(board):
    out = chat_tools.call("pipeline_run_detail", {"run_id": "f7afa3"})
    assert out["found"] is False and set(out["candidates"]) == {GATED, GATED_TWIN}


@pytest.mark.parametrize("rid", ["deadbeef0000", "abcdef", "not a run", "", "12345"])
def test_an_unknown_id_is_not_found_not_a_crash(board, rid):
    out = chat_tools.call("pipeline_run_detail", {"run_id": rid})
    assert out["found"] is False and out["error"]


def test_run_detail_keeps_the_newest_log_entries_and_says_it_cut(board):
    out = chat_tools.call("pipeline_run_detail", {"run_id": FAILED})
    assert len(out["log"]) == chat_tools.LOG_MAX
    assert out["log"][-1]["what"] == "step 49"
    assert "40 of 50" in out["log_truncated"]
    assert "arg_fields" not in out


# ── approval_queue ───────────────────────────────────────────────────────────────────────────

def test_approval_queue_matches_the_cockpits_pending_runs(board):
    out = chat_tools.call("approval_queue")
    assert [r["run_id"] for r in out["pending"]] == [r["run_id"] for r in server._pending_runs()]
    item = out["pending"][0]
    assert item["run_id"] == GATED and item["action"] == "send_message"
    assert item["target"] == "josias@example.com" and item["preview"] == "Here is the brief."
    assert item["age_s"] > 0


def test_approval_queue_empty_is_an_empty_list(board, monkeypatch):
    monkeypatch.setattr(server, "recent_runs", lambda limit=25: [])
    assert chat_tools.call("approval_queue") == {"pending": [], "count": 0}


# ── autopilot ────────────────────────────────────────────────────────────────────────────────

def test_autopilot_runs_returns_the_supervisors_runs(board):
    out = chat_tools.call("autopilot_runs")
    assert out["runs"] == SUP_RUNS and out["count"] == 2
    assert board["calls"][-1] == ("GET", "/runs?limit=20", None)


def test_autopilot_runs_filters_by_project(board):
    out = chat_tools.call("autopilot_runs", {"project": "runematch", "limit": 5})
    assert [r["task"] for r in out["runs"]] == ["648"]
    assert out["runs"][0]["result"] == "autopilot: result ready to merge"


def test_autopilot_status_and_config_match_the_supervisor(board):
    st = chat_tools.call("autopilot_status")
    assert st["projects"]["nomad"]["state"] == "needs you"
    assert st["spend"] == {"today": 3.5}
    assert chat_tools.call("autopilot_config")["config"] == SUP_CONFIG["config"]


def test_a_supervisor_that_is_down_is_said_so(monkeypatch):
    """Through the engine's real proxy call, at a port nothing listens on."""
    monkeypatch.setattr(server, "AUTOPILOT_URL", "http://127.0.0.1:9")
    for name in ("autopilot_status", "autopilot_config", "autopilot_runs"):
        assert server.AUTOPILOT_DOWN in chat_tools.call(name)["error"]


# ── system_health ────────────────────────────────────────────────────────────────────────────

TELEMETRY = {"cpu_percent": 12.0, "mem_percent": 40.0, "source": "proc",
             "gpu": {"name": "RTX 5090", "util": 3.0, "mem_used": 1000.0, "mem_total": 32000.0}}


@pytest.fixture
def watch(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "_meta": {"v": 1},
        "litellm": {"ok": True, "detail": "200", "checked": "2026-10-01T10:00:00"},
        "qdrant": {"ok": False, "detail": "refused", "fails": 3,
                   "since": "2026-10-01T09:00:00", "checked": "2026-10-01T10:00:00"}}))
    monkeypatch.setattr(server, "WATCH_STATE", str(path))
    monkeypatch.setattr(server, "_proc_telemetry", lambda: TELEMETRY)
    return path


def test_system_health_matches_the_services_endpoint(watch):
    out = chat_tools.call("system_health")
    assert out["services"] == server._services()       # what GET /api/services serves
    by = {s["name"]: s for s in out["services"]["services"]}
    assert by["litellm"]["up"] is True and by["qdrant"]["up"] is False
    assert by["qdrant"]["down_since"] == "2026-10-01T09:00:00"
    assert out["telemetry"] == TELEMETRY
    assert out["engine"]["sweep_age_s"] >= 0 and "runtime" in out["engine"]


def test_a_stale_watch_file_is_unknown_never_up(watch):
    old = time.time() - server.WATCH_STALE_S - 60
    os.utime(watch, (old, old))
    out = chat_tools.call("system_health")
    assert {s["up"] for s in out["services"]["services"]} == {None}
    assert out["services"]["stale"] is True


# ── read-only ────────────────────────────────────────────────────────────────────────────────

def test_every_tool_runs_without_a_single_write(board, watch):
    """All seven, called with arguments: no NocoDB write, and only GETs to the supervisor."""
    args = {"pipeline_runs": {"status": "failed"}, "pipeline_run_detail": {"run_id": FAILED},
            "autopilot_runs": {"project": "nomad"}}
    for name in chat_tools.TOOLS:
        out = chat_tools.call(name, args.get(name))
        assert "error" not in out or name == "pipeline_run_detail", (name, out)
    assert board["writes"] == []
    assert board["calls"] and {m for m, _, _ in board["calls"]} == {"GET"}


WRITE_NAMES = {"create", "update", "delete", "create_table", "comment", "move", "set_run",
               "transition", "resume", "capture", "trigger", "_dispatch", "_autopilot_reply",
               "urlopen", "Request", "open", "run_ci", "execute"}


def test_the_module_calls_no_write_and_sends_no_post():
    """A code check on top of the runtime one: the module never names a write, never says POST,
    and reaches the supervisor only through `_supervisor_get`, which fixes the method to GET."""
    src = inspect.getsource(chat_tools)
    tree = ast.parse(src)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            called.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    assert not called & WRITE_NAMES, called & WRITE_NAMES
    assert "POST" not in src and "PUT" not in src and "PATCH" not in src
    sup = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute) and n.func.attr == "_autopilot"]
    assert len(sup) == 1 and sup[0].args[0].value == "GET"
