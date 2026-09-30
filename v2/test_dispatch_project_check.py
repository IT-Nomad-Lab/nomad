"""#851 · a build's project is checked before the gate, and the approved plan reaches the Builder.

The Manager wrote free text into a dispatch_build's `args.project` ('claude-bridge', a GitHub
owner/repo, a file name). The run looked reasonable at the gate and failed only after approval,
with "Unknown project" or "matches 6 projects". Now the engine asks the dispatcher's registry while
drafting, writes a match back as the exact id, and every gate surface says so when it does not
resolve. Separately, `plan` never left the engine: only the one-line `task` was dispatched.

Run: python3 -m pytest v2/test_dispatch_project_check.py -q   (engine deps live in the container)
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import engine as engine_mod  # noqa: E402
import mcp_dev  # noqa: E402
import server  # noqa: E402
from engine import Engine  # noqa: E402
from test_engine_states import FakeDB  # noqa: E402


class _Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b


# ── mcp_dev: what goes to the dispatcher, and what comes back ────────────────────────────────

def test_the_approved_plan_is_sent_with_the_task(monkeypatch):
    sent = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, **json)
        return _Resp({"ok": True, "summary": "s", "branch": "feat/x"})
    monkeypatch.setattr(mcp_dev.requests, "post", post)
    mcp_dev.dispatch_build_impl("nomad", "add x", "r1", plan="1. add x\n2. test x")
    assert sent["url"].endswith("/dispatch")
    assert sent["task"] == "add x" and sent["plan"] == "1. add x\n2. test x"


def test_check_project_reads_the_dispatchers_answer(monkeypatch):
    replies = iter([_Resp({"ok": True, "id": "nomad-crm", "name": "NOMAD CRM"}),
                    _Resp({"ok": False, "reason": "unknown", "error": "Unknown project 'x'",
                           "candidates": ["nomad", "nomad-crm"]}),
                    _Resp({"error": "not found"}, 404)])
    monkeypatch.setattr(mcp_dev.requests, "post", lambda *a, **k: next(replies))
    assert mcp_dev.check_project("NOMAD CRM") == {"ok": True, "id": "nomad-crm", "name": "NOMAD CRM",
                                                  "subdir": "", "target": "nomad-crm"}
    bad = mcp_dev.check_project("x")
    assert bad["ok"] is False and bad["reason"] == "unknown"
    assert bad["candidates"] == ["nomad", "nomad-crm"]
    # An older dispatcher without /resolve: unchecked, which is not the same as fine.
    assert mcp_dev.check_project("nomad")["ok"] is None


def test_check_project_says_unchecked_when_the_dispatcher_is_down(monkeypatch):
    def post(*a, **k):
        raise mcp_dev.requests.ConnectionError("refused")
    monkeypatch.setattr(mcp_dev.requests, "post", post)
    got = mcp_dev.check_project("nomad")
    assert got["ok"] is None and "unreachable" in got["error"]


# ── the engine: checked while drafting, before the gate ──────────────────────────────────────

class _DevSpec:
    lane = name = "dev"

    def propose(self, intent, target, run_id):
        return {"action": "dispatch_build",
                "args": {"project": target, "task": intent, "plan": "the plan", "run_id": run_id}}


def _eng(monkeypatch, target, check):
    e = Engine(db=FakeDB())
    e.specialists = {"dev": _DevSpec(), "comms": _DevSpec()}
    asked, prompts = [], []
    e.check_project = lambda p: asked.append(p) or check
    e.project_ids = lambda: ["nomad", "nomad-crm", "civis-poc"]

    def chat_json(model, system, user):
        prompts.append(system)
        return {"lane": "dev", "intent": "fix the boot script", "target": target, "confidence": 0.9}
    monkeypatch.setattr(engine_mod.llm, "chat_json", chat_json)
    monkeypatch.setattr(engine_mod.memory, "recall", lambda db, q: "")
    return e, asked, prompts


def test_an_unresolved_project_is_flagged_on_the_proposal(monkeypatch):
    check = {"ok": False, "reason": "unknown", "error": "Unknown project",
             "candidates": ["nomad", "nomad-crm"]}
    e, asked, _ = _eng(monkeypatch, "fix-sshd-boot.ps1 (likely rogue01/NOMAD host repo)", check)
    r = e.start_run("fix the sshd boot script")
    assert r["status"] == "awaiting-approval"
    # Checked after routing, again after the re-ask gave the same name back, and on the draft.
    assert set(asked) == {"fix-sshd-boot.ps1 (likely rogue01/NOMAD host repo)"}
    assert r["proposal"]["project_check"] == check
    # Not rewritten into a guess: the gate shows what the Manager wrote, flagged.
    assert r["proposal"]["args"]["project"].startswith("fix-sshd-boot.ps1")


def test_a_resolved_project_is_written_back_as_the_registry_id(monkeypatch):
    e, _, _ = _eng(monkeypatch, "NOMAD CRM", {"ok": True, "id": "nomad-crm", "name": "NOMAD CRM"})
    r = e.start_run("add a field to the crm")
    assert r["proposal"]["args"]["project"] == "nomad-crm"
    payload = json.loads(e.db.find("comms", "run_id", r["run_id"])["payload"])
    assert payload["args"]["project"] == "nomad-crm" and payload["project_check"]["ok"] is True


def test_a_folder_target_is_written_back_with_its_repo(monkeypatch):
    """#906: 'claude-bridge' is a folder of nomad. It resolves without a re-ask, and the gate shows
    'nomad/claude-bridge', which the dispatcher resolves to the same repo and folder at build time."""
    check = {"ok": True, "id": "nomad", "name": "NOMAD", "subdir": "claude-bridge",
             "target": "nomad/claude-bridge"}
    e, asked, prompts = _eng(monkeypatch, "claude-bridge", check)
    r = e.start_run("Alert Zulip when LiteLLM falls back")
    assert r["proposal"]["args"]["project"] == "nomad/claude-bridge"
    assert len(prompts) == 1                      # routing only, no re-ask
    assert r["proposal"]["project_check"]["ok"] is True


def test_the_manager_is_told_to_use_registry_ids(monkeypatch):
    e, _, prompts = _eng(monkeypatch, "nomad", {"ok": True, "id": "nomad", "name": "NOMAD"})
    e.start_run("tidy the engine")
    assert "registry id" in prompts[0] and "nomad, nomad-crm, civis-poc" in prompts[0]


# ── the re-ask: an unresolved dev target gets one more try before drafting ───────────────────

class _Registry:
    """The dispatcher's /resolve over a fixed id list, recording what it was asked."""

    def __init__(self, ids=("nomad", "nomad-crm", "civis-poc"), down=False):
        self.ids, self.down, self.asked = ids, down, []

    def __call__(self, p):
        self.asked.append(p)
        if self.down:
            return {"ok": None, "error": "dispatcher unreachable"}
        if p in self.ids:
            return {"ok": True, "id": p, "name": p.upper()}
        return {"ok": False, "reason": "unknown", "error": f"Unknown project '{p}'",
                "candidates": list(self.ids)}


def _reask_eng(monkeypatch, first, second, reg):
    e = Engine(db=FakeDB())
    drafted = []

    class Spec(_DevSpec):
        def propose(self, intent, target, run_id):
            drafted.append(target)
            return super().propose(intent, target, run_id)
    e.specialists = {"dev": Spec(), "comms": Spec()}
    e.check_project = reg
    e.project_ids = lambda: list(reg.ids)
    prompts = []

    def chat_json(model, system, user):
        prompts.append((system, user))
        if len(prompts) == 1:
            return {"lane": "dev", "intent": "alert on fallback", "target": first, "confidence": 0.9}
        return {"target": second}
    monkeypatch.setattr(engine_mod.llm, "chat_json", chat_json)
    monkeypatch.setattr(engine_mod.memory, "recall", lambda db, q: "")
    return e, drafted, prompts


def test_an_unknown_target_is_re_asked_and_the_plan_drafted_for_the_answer(monkeypatch):
    reg = _Registry()
    e, drafted, prompts = _reask_eng(monkeypatch, "claude-bridge", "nomad", reg)
    r = e.start_run("Alert Zulip when LiteLLM falls back")
    assert drafted == ["nomad"]
    assert r["proposal"]["args"]["project"] == "nomad"
    assert r["proposal"]["project_check"]["ok"] is True
    # The re-ask names what was wrong and what to pick from.
    system, user = prompts[1]
    assert "claude-bridge" in user and "Unknown project 'claude-bridge'" in user
    assert "nomad, nomad-crm, civis-poc" in system
    assert any(ep["what"] == "manager re-picked project" for ep in e.db.list("episodic", 500))


def test_a_second_unknown_answer_still_reaches_the_gate_flagged(monkeypatch):
    e, drafted, prompts = _reask_eng(monkeypatch, "claude-bridge", "mission-control", _Registry())
    r = e.start_run("Alert Zulip when LiteLLM falls back")
    assert len(prompts) == 2 and drafted == ["claude-bridge"]
    assert r["status"] == "awaiting-approval"
    assert r["proposal"]["project_check"]["ok"] is False
    assert r["proposal"]["args"]["project"] == "claude-bridge"


def test_none_from_the_re_ask_is_not_checked_as_a_project(monkeypatch):
    reg = _Registry()
    e, drafted, _ = _reask_eng(monkeypatch, "claude-bridge", "none", reg)
    e.start_run("Alert Zulip when LiteLLM falls back")
    assert "none" not in reg.asked and drafted == ["claude-bridge"]


def test_no_re_ask_when_the_target_resolves_or_the_dispatcher_is_down(monkeypatch):
    for reg, target in ((_Registry(), "nomad"), (_Registry(down=True), "claude-bridge")):
        e, drafted, prompts = _reask_eng(monkeypatch, target, "nomad", reg)
        e.start_run("tidy the engine")
        assert len(prompts) == 1 and drafted == [target]


def test_non_dev_lanes_are_not_checked(monkeypatch):
    reg = _Registry()
    e, _, prompts = _reask_eng(monkeypatch, "a@b.c", "nomad", reg)
    e.specialists["comms"] = type("C", (), {"propose": lambda self, intent, target, run_id:
                                            {"action": "send_message", "args": {"to": target}}})()
    monkeypatch.setattr(engine_mod.llm, "chat_json",
                        lambda m, s, u: prompts.append(1) or {"lane": "comms", "intent": "hi",
                                                               "target": "a@b.c"})
    e.start_run("say hi to a@b.c")
    assert reg.asked == [] and len(prompts) == 1


# ── memory: a project miss is not context ─────────────────────────────────────────────────────

def test_recall_leaves_out_project_resolution_failures():
    import memory

    class DB:
        def list(self, *a, **k):
            return [
                {"agent": "dev", "what": "dispatch_build fallback alert", "created_at": "3",
                 "outcome": "FAILED: Unknown project 'claude-bridge'. Known: ['NOMAD']"},
                {"agent": "dev", "what": "dispatch_build fallback alert", "created_at": "2",
                 "outcome": "FAILED: 'IT-Nomad-Lab/nomad' matches 6 projects (NOMAD at /x)"},
                {"agent": "dev", "what": "dispatch_build fallback alert", "created_at": "1",
                 "outcome": "dispatch_build executed: added the callback [branch feat/x]"}]
    got = memory.recall(DB(), "fallback alert")
    assert "claude-bridge" not in got and "matches 6" not in got
    assert "added the callback" in got


# ── the gate: cockpit feed, run detail, chat preview ─────────────────────────────────────────

UNRESOLVED = {"action": "dispatch_build", "target": "claude-bridge",
              "args": {"project": "claude-bridge", "task": "fix it", "plan": "p"},
              "project_check": {"ok": False, "reason": "unknown", "error": "Unknown project",
                                "candidates": ["nomad", "nomad-crm"]}}


def test_project_warning_says_what_is_wrong_and_lists_candidates():
    w = server.project_warning(UNRESOLVED)
    assert "'claude-bridge'" in w and "not a project in the dispatcher's registry" in w
    assert "Candidates: nomad, nomad-crm." in w
    amb = {**UNRESOLVED, "project_check": {**UNRESOLVED["project_check"], "reason": "ambiguous"}}
    assert "more than one registry project" in server.project_warning(amb)


def test_project_warning_is_quiet_when_resolved_or_not_a_build():
    assert server.project_warning({**UNRESOLVED, "project_check": {"ok": True, "id": "nomad"}}) == ""
    assert server.project_warning({"action": "send_message", "args": {"to": "a@b.c"}}) == ""


def test_an_unchecked_build_says_so():
    """A run drafted before the check existed, or with the dispatcher down, is not passed as fine."""
    old = {k: v for k, v in UNRESOLVED.items() if k != "project_check"}
    assert "not checked" in server.project_warning(old)
    down = {**UNRESOLVED, "project_check": {"ok": None, "error": "dispatcher unreachable"}}
    assert "not checked" in server.project_warning(down)
    assert "dispatcher unreachable" in server.project_warning(down)


class _FeedDB:
    def __init__(self, payload):
        self.row = {"Id": 1, "run_id": "r1", "lane": "dev", "from_agent": "manager",
                    "type": "proposal", "status": "awaiting-approval",
                    "created_at": "2026-09-28T10:00:00", "payload": json.dumps(payload)}

    def list(self, *a, **k):
        return [self.row]

    def find(self, *a):
        return self.row


def test_the_cockpit_feed_and_detail_carry_the_warning(monkeypatch):
    monkeypatch.setattr(server, "db", _FeedDB(UNRESOLVED))
    monkeypatch.setattr(server, "_delivery", lambda rid: [])
    monkeypatch.setattr(server, "_artifacts", lambda rid: [])
    [run] = server.recent_runs()
    assert "Candidates: nomad, nomad-crm" in run["proposal"]["warning"]
    d = server.run_detail("r1")
    assert d["project_warning"] == run["proposal"]["warning"]
    # And the page draws both.
    assert "p.warning" in server.COCKPIT and "r.project_warning" in server.COCKPIT


def test_the_chat_preview_warns_before_approval(monkeypatch):
    monkeypatch.setattr(server, "db", _FeedDB(UNRESOLVED))
    monkeypatch.setattr(server, "_delivery", lambda rid: [])
    monkeypatch.setattr(server, "_artifacts", lambda rid: [])
    [run] = server.recent_runs()
    d = server.run_detail("r1")
    text = server._chat_preview(run, d, d["proposal_hash"])
    assert "claude-bridge" in text and "⚠" in text and "Candidates: nomad, nomad-crm" in text


def test_the_chat_preview_names_the_resolved_repo(monkeypatch):
    ok = {**UNRESOLVED, "target": "NOMAD CRM", "args": {**UNRESOLVED["args"], "project": "nomad-crm"},
          "project_check": {"ok": True, "id": "nomad-crm", "name": "NOMAD CRM"}}
    monkeypatch.setattr(server, "db", _FeedDB(ok))
    monkeypatch.setattr(server, "_delivery", lambda rid: [])
    monkeypatch.setattr(server, "_artifacts", lambda rid: [])
    [run] = server.recent_runs()
    d = server.run_detail("r1")
    text = server._chat_preview(run, d, d["proposal_hash"])
    assert "→ nomad-crm" in text and "⚠" not in text
    assert d["project"]["repo"] == "nomad-crm"
