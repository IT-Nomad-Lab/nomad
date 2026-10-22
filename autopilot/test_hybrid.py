"""Tests for autopilot/hybrid.py and the `execution:` block (#1488).

The controller is a fake here: agent_execution has its own suite. These tests hold the pool to
the WorkerPool contract the scheduler relies on, and the composition to the real contracts.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot/test_hybrid.py -q
"""
import json
import shutil
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, NOMAD)

from agent_execution.contracts import AttemptState as S, ContractError  # noqa: E402
from agent_execution.delivery import AmbiguousResponse, TransportUnavailable, payload_digest  # noqa: E402
from agent_execution.integration import IntegrationConflict  # noqa: E402
from agent_execution.verification import VerificationFailed, VerificationUncertain  # noqa: E402
from autopilot import hybrid, projects  # noqa: E402
from autopilot import state as state_mod  # noqa: E402
from autopilot import worker as wk  # noqa: E402
from autopilot.projects import ConfigError  # noqa: E402

BASE = "a" * 40


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _registry(tmp_path, block, pid="p", repo=True):
    if repo:
        r = tmp_path / pid
        r.mkdir(exist_ok=True)
        _git(r, "init", "-q", "-b", "master")
        _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    d = tmp_path / "nomad"
    d.mkdir(exist_ok=True)
    f = d / "projects.yaml"
    f.write_text(f"projects:\n  - id: {pid}\n    path: {pid}\n    autopilot:\n"
                 + textwrap.indent(textwrap.dedent(block), "      "))
    return str(f)


def _project(tmp_path, block, **kw):
    return projects.load(_registry(tmp_path, block, **kw))[0]


HYBRID = """\
    test: pytest -q {filter}
    execution:
      mode: hybrid
      backends: [claude, codex, local]
      gates: [{id: unit, run: make check, timeout: 600}]
    """


# ---- the execution block ---------------------------------------------------------------------

def test_no_block_means_the_legacy_pool(tmp_path):
    p = _project(tmp_path, "test: pytest\n", repo=False)
    assert p.autopilot.execution is None and not hybrid.is_hybrid(p)


def test_a_hybrid_block_parses(tmp_path):
    ex = _project(tmp_path, HYBRID, repo=False).autopilot.execution
    assert (ex.mode, ex.backends, ex.privacy, ex.wall_seconds) == \
        ("hybrid", ("claude", "codex", "local"), "cloud_allowed", 3600)
    assert ex.gates == (projects.ExecGate("unit", "make check", 600, ()),)


def test_hybrid_gates_default_to_the_rendered_pre_merge_gate(tmp_path):
    ex = _project(tmp_path, "test: {unit: 'pytest -q {filter}', e2e: npm run e2e}\n"
                            "execution: {mode: hybrid}\n", repo=False).autopilot.execution
    assert [(g.id, g.run) for g in ex.gates] == [("pre-merge-1", "pytest -q"), ("pre-merge-2", "npm run e2e")]
    assert ex.backends == ("claude",)


def test_legacy_mode_keeps_the_block_but_runs_legacy(tmp_path):
    p = _project(tmp_path, "test: pytest\nexecution: {mode: legacy, backends: [local]}\n", repo=False)
    assert not hybrid.is_hybrid(p)


@pytest.mark.parametrize("block, field", [
    ("execution: hybrid\n", "autopilot.execution: expected a mapping"),
    ("execution: {mod: hybrid}\n", "autopilot.execution.mod: unknown field"),
    ("execution: {mode: fast}\n", "autopilot.execution.mode"),
    ("execution: {backends: [gpt]}\n", r"autopilot.execution.backends\[0\]"),
    ("execution: {backends: [claude, claude]}\n", "autopilot.execution.backends: a backend is listed twice"),
    ("execution: {privacy: secret}\n", "autopilot.execution.privacy"),
    ("execution: {privacy: local_only, backends: [local, claude]}\n", "local_only allows only the local"),
    ("execution: {wall_seconds: 10}\n", "autopilot.execution.wall_seconds"),
    ("execution: {wall_seconds: true}\n", "autopilot.execution.wall_seconds"),
    ("execution: {gates: [make check]}\n", r"autopilot.execution.gates\[0\]: expected a mapping"),
    ("execution: {gates: [{id: a, run: x, env: {}}]}\n", r"autopilot.execution.gates\[0\].env"),
    ("execution: {gates: [{id: a, run: x, timeout: 0}]}\n", r"autopilot.execution.gates\[0\].timeout"),
    ("execution: {gates: [{id: a, run: x, read_roots: [/]}]}\n", r"autopilot.execution.gates\[0\].read_roots"),
    ("execution: {gates: [{id: a, run: x}, {id: a, run: y}]}\n", "two gates share an id"),
    ("execution: {mode: hybrid}\n", "hybrid mode needs a gate"),
    ("test: {u: {run: x, env: {A: b}}}\nexecution: {mode: hybrid}\n", "list the hybrid gates explicitly"),
])
def test_execution_errors_name_the_field(tmp_path, block, field):
    with pytest.raises(ConfigError, match=field):
        _project(tmp_path, block, repo=False)


def test_the_real_registry_still_loads():
    assert projects.load(os.path.join(NOMAD, "projects.yaml"))


# ---- the task as the controller sees it --------------------------------------------------------

ITEM = {"subject": "Add a page", "description": "Make /about.\nLink it from home.", "priority": "Immediate"}


def test_task_spec_from_a_nomad_pm_task(tmp_path):
    p = _project(tmp_path, HYBRID, repo=False)
    spec = hybrid.task_spec(p, ITEM, BASE, "42")
    assert (spec.task_id, spec.project_id, spec.priority) == ("openproject:42", "p", "urgent")
    assert spec.requirements == ("Make /about.", "Link it from home.")
    assert [(c.id, c.check) for c in spec.acceptance_criteria] == [("unit", "make check")]
    assert spec.repository.base_commit == BASE
    assert spec.execution_policy.privacy.value == "cloud_allowed"
    assert spec.budget.wall_seconds == 3600


def test_the_revision_follows_text_and_base_not_priority(tmp_path):
    rev = lambda item, base=BASE: hybrid.revision(item, base, "p")
    assert rev(ITEM) == rev({**ITEM, "priority": "Low"})
    assert rev(ITEM) != rev({**ITEM, "description": "Make /contact."})
    assert rev(ITEM) != rev(ITEM, "b" * 40)


def test_an_empty_description_falls_back_to_the_subject(tmp_path):
    p = _project(tmp_path, HYBRID, repo=False)
    assert hybrid.task_spec(p, {**ITEM, "description": ""}, BASE, "1").requirements == ("Add a page",)


def test_long_descriptions_say_what_was_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(hybrid, "MAX_REQUIREMENT_LINES", 2)
    p = _project(tmp_path, HYBRID, repo=False)
    reqs = hybrid.task_spec(p, {**ITEM, "description": "a\nb\nc\nd"}, BASE, "1").requirements
    assert reqs[:2] == ("a", "b") and "2 more lines" in reqs[2]


# ---- tracker transport -------------------------------------------------------------------------

class FakeOP:
    def __init__(self):
        self.posted, self.fail = [], None

    def activities(self, wp):
        if self.fail:
            raise self.fail
        return [{"comment": {"raw": t}} for w, t in self.posted if w == wp]

    def comment(self, wp, text, as_=None):
        assert as_ == "dev"
        if self.fail:
            raise self.fail
        self.posted.append((wp, text))


def test_transport_finds_what_it_applied():
    op = FakeOP()
    t = hybrid.PMTransport(lambda: op, {"e1": "openproject:7"}.get)
    payload = {"status": "Done", "attempt_id": "x", "commit": "c" * 40}
    assert t.find("e1") is None
    t.apply("e1", "openproject:7", payload)
    rec = t.find("e1")
    assert (rec.effect_id, rec.payload_digest) == ("e1", payload_digest(payload))
    assert "autopilot: result" not in op.posted[0][1]     # the legacy reader would misread it


def test_transport_errors_map_to_the_delivery_contract():
    op = FakeOP()
    t = hybrid.PMTransport(lambda: op, {"e1": "openproject:7"}.get)
    with pytest.raises(AmbiguousResponse):
        t.find("unknown")
    op.fail = OSError("down")
    with pytest.raises(TransportUnavailable):
        t.find("e1")
    with pytest.raises(AmbiguousResponse):
        t.apply("e1", "openproject:7", {"status": "Blocked", "attempt_id": "x"})


def test_blocked_comment_carries_the_reason():
    text = hybrid.effect_comment("e", {"status": "Blocked", "attempt_id": "a", "reason": "gate failed",
                                       "execution_state": "cancelled"})
    assert "blocked: gate failed (cancelled)" in text and text.endswith("sha256:" + payload_digest(
        {"status": "Blocked", "attempt_id": "a", "reason": "gate failed", "execution_state": "cancelled"}))


# ---- the pool ------------------------------------------------------------------------------------

class Ctx:
    def __init__(self, pid):
        self.project_id = pid
        self.events = []

    def event(self, kind, msg):
        self.events.append((kind, msg))


class FakeLegacy:
    def __init__(self, state_dir):
        self.state_dir, self.ledger, self.start_errors = state_dir, object(), {}
        self.runs = {("other", "1"): SimpleNamespace(reported={})}
        self.calls = []
        self.project_of = None

    def start_workers(self, ctx, tasks):
        self.calls.append(("start", ctx.project_id))
        return ["legacy"]

    def collect(self, ctx, now=None):
        self.calls.append(("collect", ctx.project_id))
        return ["legacy"]

    def recover(self, ctx, stored=()):
        self.calls.append(("recover", ctx.project_id))
        return []

    def stop(self, ctx, mode):
        self.calls.append(("stop", ctx.project_id, mode))


def result(commit="c" * 40, errors=(), warnings=()):
    return SimpleNamespace(result_commit=commit, errors=errors, warnings=warnings,
                           usage=SimpleNamespace(cost_amount=None),
                           test_results=(SimpleNamespace(gate_id="unit"),))


class FakeController:
    def __init__(self):
        self.n, self.states, self.polls, self.verifies, self.calls = 0, {}, {}, {}, []
        self.results = {}
        self.start_error = {}

    def start(self, spec, backend, instructions, *, workspace, retry_authorized):
        assert retry_authorized and os.path.isabs(workspace)
        if backend in self.start_error:
            raise self.start_error[backend]
        self.n += 1
        aid = f"att-{self.n}"
        self.states[aid] = S.RUNNING
        self.calls.append(("start", spec.task_id, backend))
        return SimpleNamespace(attempt_id=aid)

    def poll(self, aid):
        step = self.polls.pop(aid, None)
        if isinstance(step, Exception):
            raise step
        if step is not None:
            self.states[aid] = step
        return None if self.states[aid] == S.RUNNING else self.results.get(aid) or result()

    def verify(self, aid, verifier):
        self.calls.append(("verify", aid, verifier))
        step = self.verifies.pop(aid, None)
        if isinstance(step, Exception):
            raise step
        self.states[aid] = S.SUCCEEDED

    def reconcile_verification(self, aid, verifier):
        self.calls.append(("reconcile", aid))

    def cancel(self, aid, reason):
        self.calls.append(("cancel", aid, reason))
        self.states[aid] = S.CANCELLED

    def recover(self, spec, backend, aid):
        self.calls.append(("recover", spec.task_id, backend, aid))

    def deliver_tracker(self, transport):
        self.calls.append(("deliver",))
        return []


class Rig:
    def __init__(self, tmp_path, block=HYBRID, available=None, background=False):
        self.project = _project(tmp_path, block)
        self.legacy = FakeLegacy(str(tmp_path / "state"))
        self.ctl = FakeController()
        self.items = {}
        self.now = [1000.0]
        self.available = available or {"claude": (True, ""), "codex": (True, ""), "local": (True, "")}
        store = SimpleNamespace(get=lambda aid: SimpleNamespace(state=self.ctl.states[aid]),
                                result=lambda aid: result())
        self.ex = hybrid.Execution(self.ctl, store, lambda pid: f"verifier:{pid}", None, ("instr",),
                                   lambda b: self.available[b])
        self.pool = hybrid.HybridPool(self.legacy, lambda: self.ex, self.fetch,
                                      project_of=lambda pid: self.project if pid == "p" else None,
                                      clock=lambda: self.now[0], background_checks=background)
        self.ctx = Ctx("p")

    def fetch(self, tid):
        if self.items.get(tid) == "down":
            raise OSError("nomad-pm down")
        return self.items.get(tid, ITEM)

    def start(self, *tids):
        return self.pool.start_workers(self.ctx, [{"task": t, "title": f"task {t}"} for t in tids])

    def ledger(self):
        f = os.path.join(self.legacy.state_dir, state_mod.LEDGER_FILE)
        return [json.loads(x) for x in open(f)] if os.path.exists(f) else []


def test_projects_that_did_not_opt_in_go_to_the_legacy_pool(tmp_path):
    rig = Rig(tmp_path)
    other = Ctx("other")
    assert rig.pool.start_workers(other, [{"task": "1"}]) == ["legacy"]
    assert rig.pool.collect(other) == ["legacy"]
    rig.pool.recover(other, [])
    rig.pool.stop(other, "kill")
    assert rig.legacy.calls == [("start", "other"), ("collect", "other"), ("recover", "other"),
                                ("stop", "other", "kill")]
    assert rig.ctl.calls == []      # the controller was never touched


def test_the_pool_exposes_the_legacy_state(tmp_path):
    rig = Rig(tmp_path)
    assert rig.pool.state_dir == rig.legacy.state_dir and rig.pool.ledger is rig.legacy.ledger
    assert rig.pool.start_errors is rig.legacy.start_errors
    rig.start("5")
    assert set(rig.pool.runs) == {("other", "1"), ("p", "5")}
    assert rig.pool.runs[("p", "5")].reported["dirty"] is False


def test_attempts_spread_across_backends_with_one_local_at_a_time(tmp_path):
    rig = Rig(tmp_path)
    started = rig.start("1", "2", "3", "4", "5")
    assert [w["backend"] for w in started] == ["claude", "codex", "local", "claude", "codex"]
    for w in started:
        assert set(w) >= {"task", "branch", "worktree", "log", "started", "deadline"}
        assert w["branch"].startswith("feat/")


def test_the_local_backend_never_runs_two_attempts(tmp_path):
    rig = Rig(tmp_path, block=HYBRID.replace("[claude, codex, local]", "[local, claude]"))
    assert [w["backend"] for w in rig.start("1", "2", "3")] == ["local", "claude", "claude"]
    rig.pool.cooling["claude"] = (rig.now[0] + 60, "test")
    assert rig.start("4") == []
    assert "local already runs an attempt" in rig.legacy.start_errors["p"][0]


def test_unavailable_backends_are_skipped_and_named(tmp_path):
    rig = Rig(tmp_path, available={"claude": (False, "no plan"), "codex": (False, "#1486"),
                                   "local": (False, ":8240 is down")})
    assert rig.start("1") == []
    (err,) = rig.legacy.start_errors["p"]
    assert "no plan" in err and "#1486" in err and ":8240 is down" in err


def test_a_backend_that_fails_to_start_cools_down(tmp_path):
    rig = Rig(tmp_path)
    rig.ctl.start_error["claude"] = ContractError("GPU occupied")
    assert rig.start("1") == []
    assert "claude did not start" in rig.legacy.start_errors["p"][0]
    assert rig.start("2")[0]["backend"] == "codex"      # claude is cooling down
    rig.now[0] += hybrid.COOLDOWN_SECS + 1
    del rig.ctl.start_error["claude"]
    assert rig.start("3")[0]["backend"] == "claude"


def test_a_dirty_worktree_is_never_started(tmp_path):
    rig = Rig(tmp_path)
    w = rig.start("1")[0]
    rig.pool.hybrid_runs.clear()
    (open(os.path.join(w["worktree"], "left.txt"), "w")).write("x")
    assert rig.start("1") == []
    assert "uncommitted changes" in rig.legacy.start_errors["p"][0]


def test_a_verified_candidate_is_reported_for_the_merge_queue(tmp_path):
    rig = Rig(tmp_path)
    w = rig.start("1")[0]
    assert rig.pool.collect(rig.ctx) == []          # still running
    rig.ctl.polls[w["attempt_id"]] = S.VERIFYING
    (out,) = rig.pool.collect(rig.ctx)
    assert (out["outcome"], out["branch"], out["worktree"], out["requeue"]) == \
        ("merge", w["branch"], w["worktree"], False)
    assert "passed unit" in out["detail"]
    assert ("verify", w["attempt_id"], "verifier:p") in rig.ctl.calls
    assert ("deliver",) in rig.ctl.calls
    assert rig.ledger()[-1]["outcome"] == "merge" and rig.ledger()[-1]["backend"] == "claude"
    assert rig.pool.hybrid_runs == {}
    assert json.load(open(w["log"]))["collected"] == "merge"


@pytest.mark.parametrize("path", ["verify", "poll"])
def test_an_attempt_that_changed_nothing_is_not_reported_for_merge(tmp_path, path):
    rig = Rig(tmp_path)
    aid = rig.start("1")[0]["attempt_id"]
    base = rig.pool.hybrid_runs[("p", "1")].base
    if path == "verify":
        rig.ctl.polls[aid] = S.VERIFYING
        rig.ex.store.result = lambda a: result(commit=base)
    else:
        rig.ctl.polls[aid] = S.SUCCEEDED
        rig.ctl.poll = lambda a: (rig.ctl.states.__setitem__(a, rig.ctl.polls.pop(a)), result(commit=base))[1]
    (out,) = rig.pool.collect(rig.ctx)
    assert out["outcome"] == "failed" and "changed nothing" in out["detail"]
    assert rig.ledger()[-1]["outcome"] == "failed"


@pytest.mark.parametrize("error, outcome, kept", [
    (VerificationFailed("independent gate failed: unit", result(errors=("gate",)), None), "failed", False),
    (IntegrationConflict("default branch conflicts"), "failed", False),
    (ContractError("dirty candidate requires a configured finalizer"), "needs_operator", False),
    (ContractError("default branch kept moving; candidate left awaiting verification"), None, True),
    (VerificationUncertain("verifier termination remains uncertain"), None, True),
])
def test_verification_outcomes(tmp_path, error, outcome, kept):
    rig = Rig(tmp_path)
    aid = rig.start("1")[0]["attempt_id"]
    rig.ctl.polls[aid] = S.VERIFYING
    rig.ctl.verifies[aid] = error
    out = rig.pool.collect(rig.ctx)
    if kept:
        assert out == [] and ("p", "1") in rig.pool.hybrid_runs
    else:
        assert out[0]["outcome"] == outcome


def test_an_uncertain_verification_is_reconciled_before_retrying(tmp_path):
    rig = Rig(tmp_path)
    aid = rig.start("1")[0]["attempt_id"]
    rig.ctl.polls[aid] = S.VERIFYING
    rig.ctl.verifies[aid] = VerificationUncertain("unsure")
    assert rig.pool.collect(rig.ctx) == []
    (out,) = rig.pool.collect(rig.ctx)
    calls = [c[0] for c in rig.ctl.calls if c[0] in ("reconcile", "verify")]
    assert calls == ["verify", "reconcile", "verify"] and out["outcome"] == "merge"


def test_a_blocked_attempt_fails_with_its_errors(tmp_path):
    rig = Rig(tmp_path)
    aid = rig.start("1")[0]["attempt_id"]
    rig.ctl.polls[aid] = S.BLOCKED
    (out,) = rig.pool.collect(rig.ctx)
    assert out["outcome"] == "failed" and "blocked" in out["detail"]
    assert rig.ledger()[-1]["outcome"] == "failed"     # feeds the failure streak


def test_a_result_warning_reaches_the_run_detail(tmp_path):
    # #1503: a Codex login refresh that was not written back must reach the operator.
    rig = Rig(tmp_path)
    aid = rig.start("1")[0]["attempt_id"]
    rig.ctl.results[aid] = result(warnings=("Codex refreshed its login during the run",))
    rig.ctl.polls[aid] = S.BLOCKED
    (out,) = rig.pool.collect(rig.ctx)
    assert "Warning: Codex refreshed its login" in out["detail"]
    assert "Codex refreshed its login" in rig.ledger()[-1]["detail"]


def test_an_unconfirmed_exit_needs_the_operator(tmp_path):
    rig = Rig(tmp_path)
    aid = rig.start("1")[0]["attempt_id"]
    rig.ctl.polls[aid] = ContractError("process exit unconfirmed; workspace remains reserved")
    (out,) = rig.pool.collect(rig.ctx)
    assert out["outcome"] == "needs_operator" and "stays reserved" in out["detail"]


def test_the_time_limit_cancels_and_requeues(tmp_path):
    rig = Rig(tmp_path)
    w = rig.start("1")[0]
    rig.now[0] = w["deadline"]
    (out,) = rig.pool.collect(rig.ctx)
    assert ("cancel", w["attempt_id"], "time limit reached") in rig.ctl.calls
    assert (out["outcome"], out["requeue"]) == ("resume", True)


def test_a_timeout_with_uncommitted_work_goes_to_the_operator(tmp_path):
    rig = Rig(tmp_path)
    w = rig.start("1")[0]
    open(os.path.join(w["worktree"], "half.txt"), "w").write("x")
    rig.now[0] = w["deadline"]
    (out,) = rig.pool.collect(rig.ctx)
    assert out["outcome"] == "needs_operator" and "uncommitted work" in out["detail"]


def test_a_cancel_the_pool_did_not_ask_for_is_a_failure(tmp_path):
    rig = Rig(tmp_path)
    aid = rig.start("1")[0]["attempt_id"]
    rig.ctl.polls[aid] = S.CANCELLED
    (out,) = rig.pool.collect(rig.ctx)
    assert out["outcome"] == "failed" and "policy changed" in out["detail"]


def test_nomad_pm_down_skips_the_poll_but_not_the_deadline(tmp_path):
    rig = Rig(tmp_path)
    w = rig.start("1")[0]
    rig.items["1"] = "down"
    rig.ctl.polls[w["attempt_id"]] = S.VERIFYING
    assert rig.pool.collect(rig.ctx) == []
    assert not any(c[0] == "verify" for c in rig.ctl.calls)
    rig.now[0] = w["deadline"]
    rig.pool.collect(rig.ctx)
    assert ("cancel", w["attempt_id"], "time limit reached") in rig.ctl.calls


def test_the_spec_is_refreshed_from_nomad_pm_each_pass(tmp_path):
    rig = Rig(tmp_path)
    rig.start("1")
    before = rig.pool.specs["openproject:1"].task_revision
    rig.items["1"] = {**ITEM, "description": "changed"}
    rig.pool.collect(rig.ctx)
    assert rig.pool.specs["openproject:1"].task_revision != before


def test_stop_kill_cancels_and_reports_the_operator(tmp_path):
    rig = Rig(tmp_path)
    aid = rig.start("1")[0]["attempt_id"]
    rig.pool.stop(rig.ctx, "finish")
    assert not any(c[0] == "cancel" for c in rig.ctl.calls)
    rig.pool.stop(rig.ctx, "kill")
    (out,) = rig.pool.collect(rig.ctx)
    assert ("cancel", aid, "stopped by the operator") in rig.ctl.calls
    assert out["outcome"] == "resume" and "stopped by the operator" in out["detail"]


def test_recover_adopts_uncollected_attempts(tmp_path):
    rig = Rig(tmp_path)
    w1, w2 = rig.start("1", "2")
    rig.ctl.polls[w2["attempt_id"]] = S.BLOCKED
    rig.pool.collect(rig.ctx)           # 2 is collected, 1 still runs
    fresh = hybrid.HybridPool(rig.legacy, lambda: rig.ex, rig.fetch,
                              project_of=lambda pid: rig.project, clock=lambda: rig.now[0])
    workers = fresh.recover(rig.ctx, [])
    assert [w["task"] for w in workers] == ["1"]
    assert ("recover", "openproject:1", w1["backend"], w1["attempt_id"]) in rig.ctl.calls
    assert set(fresh.hybrid_runs) == {("p", "1")}


def test_recover_leaves_an_unadoptable_attempt_for_the_operator(tmp_path):
    rig = Rig(tmp_path)
    rig.start("1")
    rig.ctl.recover = lambda *a: (_ for _ in ()).throw(ContractError("process ownership uncertain"))
    fresh = hybrid.HybridPool(rig.legacy, lambda: rig.ex, rig.fetch,
                              project_of=lambda pid: rig.project, clock=lambda: rig.now[0])
    assert fresh.recover(rig.ctx, []) == []
    assert any("not adopted" in m for _, m in rig.ctx.events)


def test_the_execution_is_built_only_when_a_hybrid_task_runs(tmp_path):
    built = []
    legacy = FakeLegacy(str(tmp_path / "state"))
    pool = hybrid.HybridPool(legacy, lambda: built.append(1), lambda t: ITEM,
                             project_of=lambda pid: None)
    pool.collect(Ctx("other"))
    assert built == []


# ---- the real composition ------------------------------------------------------------------------

LITELLM = """\
model_list:
  - model_name: longdoc-local
    litellm_params: {model: hosted_vllm/q, api_base: "http://host.docker.internal:8240/v1"}
  - model_name: code
    litellm_params: {model: hosted_vllm/q, api_base: "http://127.0.0.1:8240/v1"}
  - model_name: cloudy
    litellm_params: {model: gemini/x, api_base: "https://example.com/v1"}
router_settings:
  fallbacks:
    - code: ["codex"]
"""


def test_the_local_route_must_be_local_with_no_fallback(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(LITELLM)
    assert hybrid.local_only_route("longdoc-local", str(f))
    assert not hybrid.local_only_route("code", str(f))         # falls back to the cloud
    assert not hybrid.local_only_route("cloudy", str(f))       # not a local server
    assert not hybrid.local_only_route("missing", str(f))
    f.write_text(LITELLM + "litellm_settings:\n  default_fallbacks: [codex]\n")
    assert not hybrid.local_only_route("longdoc-local", str(f))
    assert not hybrid.local_only_route("longdoc-local", str(tmp_path / "none.yaml"))


def test_the_live_config_keeps_longdoc_local_local():
    # The local backend's privacy rests on this; `code` must stay refused (it reaches codex).
    assert hybrid.local_only_route(hybrid.LOCAL_ALIAS)
    assert not hybrid.local_only_route("code")


def test_the_agent_bridge_local_alias_stays_local():
    # #1504: agent-bridge labels these runs local; its alias must not fall back to the cloud.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bridge_backends", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "agent-bridge", "backends.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hybrid.local_only_route(mod.LOCAL_MODEL)


@pytest.fixture
def short_root():
    """An execution root short enough for the authority sockets; pytest's tmp_path is not."""
    d = tempfile.mkdtemp(prefix="hx", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def test_build_execution_composes_against_the_real_contracts(tmp_path, short_root, monkeypatch):
    p = _project(tmp_path, HYBRID)
    f = tmp_path / "c.yaml"
    f.write_text(LITELLM)
    monkeypatch.setattr(hybrid, "LITELLM_CONFIG", str(f))
    monkeypatch.setattr(hybrid, "LITELLM_KEY_FILE", str(tmp_path / "no-key"))
    monkeypatch.setattr(hybrid, "local_up", lambda: True)

    def no_cli(runtime=None):
        raise ContractError("installed Codex CLI not found")
    monkeypatch.setattr(hybrid, "codex_factory", no_cli)
    ex = hybrid.build_execution(lambda: [p], pm=None, root=str(short_root / "exec"),
                                runtime=str(short_root / "run"))
    assert set(ex.controller.adapters) == {"claude", "local"}
    assert [g.gate_id for g in ex.verifier_of("p").gates] == ["unit"]
    assert ex.verifier_of("p").gates[0].argv == ("/bin/sh", "-c", "make check")
    assert ex.available("claude") == (True, "")
    assert ex.available("codex") == (False, "adapter not built: ContractError: installed Codex CLI not found")
    assert not ex.available("local")[0] and "no LiteLLM key" in ex.available("local")[1]
    assert oct(os.stat(short_root / "exec").st_mode & 0o777) == "0o700"
    cloud = ex.controller.adapters["claude"].profile.environment
    assert "ANTHROPIC_AUTH_TOKEN" not in cloud and "ANTHROPIC_API_KEY" not in cloud


@pytest.mark.parametrize("props, ok", [
    ({"default_generation_settings": {"n_ctx": 32768}}, False),
    ({"default_generation_settings": {"n_ctx": 65536}}, True),
    ({"n_ctx": 65536}, True),
    ({}, True),
    (None, True),
    ("invalid JSON", True),
])
def test_local_context_preflight(tmp_path, short_root, monkeypatch, props, ok):
    import io
    import urllib.request
    p = _project(tmp_path, HYBRID.replace("[claude, codex, local]", "[local]"))
    monkeypatch.setattr(hybrid, "local_only_route", lambda *a: True)
    monkeypatch.setattr(hybrid, "litellm_key", lambda: "test-key")
    monkeypatch.setattr(hybrid, "local_up", lambda: True)

    def read(url, timeout):
        assert url == "http://127.0.0.1:8240/props" and timeout == 2
        if props is None:
            raise OSError("unreadable")
        return io.BytesIO((props if isinstance(props, str) else json.dumps(props)).encode())

    monkeypatch.setattr(urllib.request, "urlopen", read)
    ex = hybrid.build_execution(lambda: [p], pm=None, root=str(short_root / "exec"),
                                runtime=str(short_root / "run"))
    env = ex.controller.adapters["local"].profile.environment
    assert env["ENABLE_TOOL_SEARCH"] == "true"
    available, reason = ex.available("local")
    assert available is ok
    assert hybrid.LOCAL_MIN_CTX == 44192
    if not ok:
        for text in ("n_ctx=32768", "prompt estimate 36000", "output allowance 8192",
                     "llama/qwen38.sh stop", "QWEN38_CTX=65536 llama/qwen38.sh start"):
            assert text in reason
    elif props in ({}, None, "invalid JSON"):
        assert "unreadable" in reason and "unverified" in reason
    else:
        assert reason == ""


def test_master_and_main_projects_share_one_controller(tmp_path, short_root):
    # #1499: one controller used to refuse unless every hybrid project had the same default branch.
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _project(tmp_path / "a", HYBRID, pid="a")
    b = _project(tmp_path / "b", HYBRID, pid="b")
    _git(b.repo, "branch", "-m", "master", "main")
    ex = hybrid.build_execution(lambda: [a, b], pm=None, root=str(short_root / "exec"),
                                runtime=str(short_root / "run"))
    assert ex.controller.integration.branch == {os.path.realpath(a.repo): "master",
                                                os.path.realpath(b.repo): "main"}


def test_codex_is_built_with_the_controllers_policy(tmp_path, short_root, monkeypatch):
    from agent_execution import codex
    seen = {}

    def factory(**kw):
        seen.update(kw)
        return lambda agent, artifacts, store, root: ("codex adapter", agent.agent_id, str(root))
    monkeypatch.setattr(codex, "codex_adapter_factory", factory)
    p = _project(tmp_path, HYBRID)
    ex = hybrid.build_execution(lambda: [p], pm=None, root=str(short_root / "exec"),
                                runtime=str(short_root / "run"))
    assert seen == {"policy_revision": hybrid.POLICY_REVISION, "tool_profile_ref": hybrid.TOOL_PROFILE,
                    "runtime": str(short_root / "run")}
    assert ex.controller.adapters["codex"] == ("codex adapter", "codex", str(short_root / "exec"))
    assert ex.available("codex") == (True, "")


def test_a_codex_adapter_that_fails_to_build_leaves_the_rest_running(tmp_path, short_root, monkeypatch):
    def broken(agent, artifacts, store, root):
        raise ContractError("Codex trusted home must lie outside every sandbox read and write root")
    monkeypatch.setattr(hybrid, "codex_factory", lambda runtime=None: broken)
    p = _project(tmp_path, HYBRID)
    ex = hybrid.build_execution(lambda: [p], pm=None, root=str(short_root / "exec"),
                                runtime=str(short_root / "run"))
    assert "codex" not in ex.controller.adapters and ex.available("claude") == (True, "")
    assert not ex.available("codex")[0] and "trusted home" in ex.available("codex")[1]


@pytest.mark.skipif(shutil.which("codex") is None, reason="no installed Codex CLI")
def test_codex_composes_with_the_installed_cli(tmp_path, short_root):
    p = _project(tmp_path, HYBRID)
    ex = hybrid.build_execution(lambda: [p], pm=None, root=str(short_root / "exec"),
                                runtime=str(short_root / "run"))
    profile = ex.controller.adapters["codex"].profile
    assert (profile.backend, profile.policy_revision, profile.tool_profile_ref) == \
        ("codex", hybrid.POLICY_REVISION, hybrid.TOOL_PROFILE)
    assert ex.available("codex") == (True, "")


def test_build_execution_binds_sockets_under_the_runtime_dir_whatever_the_root(tmp_path, short_root):
    # #1497: the socket used to sit under the root, 2 bytes under AF_UNIX's limit on the live host.
    p = _project(tmp_path, HYBRID)
    root = tmp_path / ("x" * 120) / "exec"
    ex = hybrid.build_execution(lambda: [p], pm=None, root=str(root), runtime=str(short_root / "run"))
    sockets = list((short_root / "run" / "nomad-exec").iterdir())
    assert len(sockets) == 1 and oct(sockets[0].stat().st_mode & 0o777) == "0o700"
    assert oct((short_root / "run" / "nomad-exec").stat().st_mode & 0o777) == "0o700"
    assert (root / "attempts.db").exists() and ex.available("claude") == (True, "")


def test_build_execution_refuses_a_runtime_dir_too_long_for_the_socket(tmp_path, short_root):
    p = _project(tmp_path, HYBRID)
    runtime = short_root / ("r" * 60)
    with pytest.raises(hybrid.HybridError, match="AF_UNIX allows 107"):
        hybrid.build_execution(lambda: [p], pm=None, root=str(short_root / "exec"), runtime=str(runtime))
    assert not (short_root / "exec" / "attempts.db").exists()


def test_build_execution_refuses_without_a_hybrid_project(tmp_path):
    p = _project(tmp_path, "test: pytest\n")
    with pytest.raises(hybrid.HybridError, match="opts in"):
        hybrid.build_execution(lambda: [p], pm=None, root=str(tmp_path / "exec"))


def test_build_scheduler_is_unchanged_until_a_project_opts_in(tmp_path, monkeypatch):
    from autopilot import supervisor
    from autopilot.scheduler import AutopilotScheduler
    plain = _project(tmp_path, "test: pytest\n", repo=False)
    monkeypatch.setattr(projects, "hub_load", lambda *a: [plain])
    monkeypatch.setattr(hybrid, "build_pool", lambda *a: pytest.fail("built a hybrid pool"))
    sch = supervisor.build_scheduler()
    assert isinstance(sch, AutopilotScheduler) and type(sch.pool) is wk.WorkerPool

    (tmp_path / "h").mkdir()
    opted = _project(tmp_path / "h", HYBRID, repo=False)
    sentinel = object()
    monkeypatch.setattr(projects, "hub_load", lambda *a: [plain, opted])
    monkeypatch.setattr(hybrid, "build_pool", lambda legacy, listing, pm: sentinel)
    assert supervisor.build_scheduler().pool is sentinel


def test_a_broken_registry_keeps_the_legacy_pool(tmp_path, monkeypatch):
    from autopilot import supervisor

    def broken(*a):
        raise ConfigError("bad")
    monkeypatch.setattr(projects, "hub_load", broken)
    assert type(supervisor.build_scheduler().pool) is wk.WorkerPool


# ---- backup --------------------------------------------------------------------------------------

def test_backup_copies_online_and_keeps_fourteen_days(tmp_path):
    db = tmp_path / "attempts.db"
    con = sqlite3.connect(db)
    con.execute("pragma journal_mode=wal")
    con.execute("create table t (x)")
    con.execute("insert into t values (1)")
    con.commit()                                    # left open: the supervisor would hold it
    dest = tmp_path / "backups"
    dest.mkdir()
    old, mine = dest / "attempts-20200101-000000.db", dest / "notes.db"
    old.write_text("old")
    mine.write_text("keep")
    for f in (old, mine):
        os.utime(f, (0, 0))
    out = hybrid.backup(db, dest, now=time.time())
    assert sqlite3.connect(out).execute("select x from t").fetchall() == [(1,)]
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    assert not old.exists() and mine.exists()       # only this job's own copies are pruned
    con.close()


def test_backup_without_a_store_says_so(tmp_path):
    with pytest.raises(hybrid.HybridError, match="no attempt store"):
        hybrid.backup(tmp_path / "missing.db", tmp_path / "b")


# ---- staged rollback CLI (#1496) -----------------------------------------------------------------

def _version(db):
    return sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0]


def test_rollback_walks_begin_handback_schema_forward_resume(tmp_path):
    from agent_execution.store import AttemptStore, OLDEST_ROLLBACK_VERSION, SCHEMA_VERSION
    db, gpu = tmp_path / "attempts.db", tmp_path / "gpu_guard_state.json"
    AttemptStore(db).close()
    rb = lambda cmd, **kw: hybrid.rollback(cmd, db=db, gpu_paths=(gpu,), actor="op", **kw)
    assert "blocker: new dispatch is still enabled" in rb("status")
    with pytest.raises(hybrid.HybridError, match="new dispatch is still enabled"):
        rb("handback")
    out = rb("begin", reason="drill")
    assert out[0] == "new hybrid dispatch disabled" and "no blockers" in out[-1]
    gpu.write_text("{}")                            # a pending gpu_guard restore blocks both
    for cmd, kw in (("handback", {}), ("schema", {"version": OLDEST_ROLLBACK_VERSION})):
        with pytest.raises(hybrid.HybridError, match="GPU restoration pending"):
            rb(cmd, reason="drill", **kw)
    gpu.unlink()
    assert "handback allowed" in rb("handback")[0]
    assert rb("schema", version=OLDEST_ROLLBACK_VERSION, reason="drill")[0].startswith(
        f"schema at v{OLDEST_ROLLBACK_VERSION}")
    # Rolled back: status reads the version without opening (which would migrate forward).
    assert "was not opened" in rb("status")[0]
    for cmd in ("begin", "resume", "handback"):
        with pytest.raises(hybrid.HybridError, match="would migrate it forward"):
            rb(cmd, reason="x")
    assert _version(db) == OLDEST_ROLLBACK_VERSION
    assert rb("forward") == [f"schema v{SCHEMA_VERSION} (was v{OLDEST_ROLLBACK_VERSION})"]
    assert rb("resume", reason="drill done") == ["hybrid dispatch re-enabled"]
    assert "dispatch: enabled" in rb("status")


def test_rollback_refuses_without_a_store_or_a_reason(tmp_path):
    from agent_execution.store import AttemptStore
    with pytest.raises(hybrid.HybridError, match="no attempt store"):
        hybrid.rollback("status", db=tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()   # status never creates a store
    AttemptStore(tmp_path / "a.db").close()
    for cmd in ("begin", "resume", "schema"):
        with pytest.raises(hybrid.HybridError, match="needs --reason"):
            hybrid.rollback(cmd, db=tmp_path / "a.db", version=1, reason=" ")


@pytest.mark.parametrize("args, code", [(["rollback"], 2), (["rollback", "drop"], 2),
                                        (["rollback", "schema"], 2), (["rollback", "schema", "x"], 2),
                                        (["rollback", "begin"], 1)])
def test_rollback_main_usage_and_refusals(monkeypatch, tmp_path, capsys, args, code):
    from agent_execution.store import AttemptStore
    AttemptStore(tmp_path / "attempts.db").close()
    monkeypatch.setattr(hybrid, "EXEC_DIR", str(tmp_path))
    assert hybrid.main(["hybrid"] + args) == code
    err = capsys.readouterr().err
    assert ("usage:" in err) if code == 2 else ("refused: rollback begin needs --reason" in err)


def test_rollback_main_runs_against_the_exec_dir(monkeypatch, tmp_path, capsys):
    from agent_execution.store import AttemptStore
    AttemptStore(tmp_path / "attempts.db").close()
    monkeypatch.setattr(hybrid, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(hybrid, "MEDIA_STATE", str(tmp_path / "none.json"))
    assert hybrid.main(["hybrid", "rollback", "begin", "--reason", "drill"]) == 0
    assert "new hybrid dispatch disabled" in capsys.readouterr().out
    (tmp_path / "none.json").write_text("{}")       # the real gpu_guard path is checked by default
    assert hybrid.main(["hybrid", "rollback", "handback"]) == 1
    assert "GPU restoration pending" in capsys.readouterr().err


# ---- prepare (dependencies for the offline gates) ------------------------------------------------

def _prepared(command):
    return HYBRID.rstrip(" ") + f"      prepare: {command}\n"


def _commit(repo, name, text):
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", name)


def test_prepare_parses_and_defaults_off(tmp_path):
    assert _project(tmp_path, HYBRID, repo=False).autopilot.execution.prepare == ""
    assert _project(tmp_path, _prepared("uv sync --locked"), repo=False).autopilot.execution.prepare \
        == "uv sync --locked"
    with pytest.raises(ConfigError, match="prepare"):
        _project(tmp_path, _prepared("[uv]"), repo=False)


def test_prepare_runs_in_the_worktree_before_the_agent(tmp_path):
    rig = Rig(tmp_path, block=_prepared("touch .deps"))
    _commit(tmp_path / "p", ".gitignore", ".deps\n")
    (w,) = rig.start("1")
    assert os.path.exists(os.path.join(w["worktree"], ".deps"))
    assert rig.ctl.calls[0][0] == "start"


@pytest.mark.parametrize("command, why", [
    ("'echo boom >&2; exit 3'", "prepare failed (exit 3): boom"),
    ("'echo x > tracked.txt'", "prepare changed tracked files"),
])
def test_a_failed_prepare_starts_nothing(tmp_path, command, why):
    rig = Rig(tmp_path, block=_prepared(command))
    _commit(tmp_path / "p", "tracked.txt", "a")
    assert rig.start("1") == []
    assert why in rig.legacy.start_errors["p"][0]
    assert rig.ctl.calls == []
    assert "claude" not in rig.pool.cooling        # not the backend's fault


def test_a_hung_prepare_times_out(tmp_path):
    with pytest.raises(hybrid.HybridError, match="prepare took over 1s"):
        hybrid.prepare("sleep 5", str(tmp_path), timeout=1)


# ---- finalization scope ----------------------------------------------------------------------

def test_the_task_scope_permits_every_change_the_finalizer_sees(tmp_path):
    from agent_execution.finalization import _permitted
    rig = Rig(tmp_path)
    _commit(tmp_path / "p", "pkg.txt", "a")
    (w,) = rig.start("1")
    ws = Path(w["worktree"])
    (ws / "pkg.txt").write_text("b")                       # modified, top level
    (ws / "src" / "deep").mkdir(parents=True)
    (ws / "src" / "deep" / "new module.py").write_text("x")  # new directory, space in the name
    (ws / "NEW.md").write_text("n")                        # new top-level file
    branch, allowed = hybrid.task_scope(None, SimpleNamespace(workspace=str(ws)))
    assert branch == w["branch"]
    for name in ("pkg.txt", "src/deep/new module.py", "NEW.md"):
        assert _permitted(name, allowed), (name, allowed)
    assert not _permitted("../outside", allowed) and not _permitted("/etc/passwd", allowed)


def test_the_task_scope_refuses_a_workspace_off_a_task_branch(tmp_path):
    rig = Rig(tmp_path)
    with pytest.raises(hybrid.HybridError, match="not on a task branch"):
        hybrid.task_scope(None, SimpleNamespace(workspace=str(tmp_path / "p")))


# ---- verification off the pass (#1488) ----------------------------------------------------


def _slow_verify(rig, aid, outcome=None):
    """verify() blocks until released, like gates that take minutes; returns (entered, release)."""
    entered, release = threading.Event(), threading.Event()
    inner, outcome = rig.ctl.verify, [outcome] if outcome is not None else []

    def verify(a, verifier):
        if a != aid:
            return inner(a, verifier)
        entered.set()
        assert release.wait(10)
        if outcome:
            rig.ctl.verifies[a] = outcome.pop()      # the first check only
        return inner(a, verifier)
    rig.ctl.verify = verify
    return entered, release


def _collect_until(rig, n=200):
    for _ in range(n):
        done = rig.pool.collect(rig.ctx)
        if done:
            return done
        time.sleep(0.01)
    return []


def test_the_pass_does_not_wait_for_the_gates(tmp_path):
    rig = Rig(tmp_path, background=True)
    w1, w2 = rig.start("1", "2")
    entered, release = _slow_verify(rig, w1["attempt_id"])
    rig.ctl.polls[w1["attempt_id"]] = S.VERIFYING
    assert rig.pool.collect(rig.ctx) == []          # returns while the gates run
    assert entered.wait(5)
    rig.ctl.polls[w2["attempt_id"]] = S.BLOCKED
    (out,) = rig.pool.collect(rig.ctx)              # the other attempt is still followed
    assert out["task"] == "2"
    assert entered.is_set() and ("p", "1") in rig.pool.hybrid_runs     # gates still running
    release.set()
    (out,) = _collect_until(rig)
    assert (out["task"], out["outcome"]) == ("1", "merge")
    assert [c for c in rig.ctl.calls if c[0] == "verify"] == [("verify", w1["attempt_id"], "verifier:p")]
    assert rig.pool.hybrid_runs == {}


def test_a_failed_check_in_the_background_is_reported(tmp_path):
    rig = Rig(tmp_path, background=True)
    aid = rig.start("1")[0]["attempt_id"]
    _, release = _slow_verify(rig, aid, VerificationFailed("gate unit failed", result(), None))
    rig.ctl.polls[aid] = S.VERIFYING
    assert rig.pool.collect(rig.ctx) == []
    release.set()
    (out,) = _collect_until(rig)
    assert out["outcome"] == "failed" and "gate unit failed" in out["detail"]


def test_an_unsettled_background_check_reports_its_event_and_retries(tmp_path):
    rig = Rig(tmp_path, background=True)
    aid = rig.start("1")[0]["attempt_id"]
    _, release = _slow_verify(rig, aid, VerificationUncertain("checker not proven stopped"))
    rig.ctl.polls[aid] = S.VERIFYING
    rig.pool.collect(rig.ctx)
    release.set()
    for _ in range(200):
        if any("not settled" in m for _, m in rig.ctx.events):
            break
        rig.pool.collect(rig.ctx)
        time.sleep(0.01)
    assert any("not settled" in m for _, m in rig.ctx.events)
    (out,) = _collect_until(rig)                    # next check reconciles, then passes
    assert out["outcome"] == "merge"
    names = [c[0] for c in rig.ctl.calls if c[0] in ("verify", "reconcile")]
    assert names == ["verify", "reconcile", "verify"]


@pytest.mark.parametrize("how", ["kill", "deadline"])
def test_nothing_cancels_an_attempt_while_its_gates_run(tmp_path, how):
    rig = Rig(tmp_path, background=True)
    aid = rig.start("1")[0]["attempt_id"]
    _, release = _slow_verify(rig, aid, VerificationUncertain("checker not proven stopped"))
    rig.ctl.polls[aid] = S.VERIFYING
    rig.pool.collect(rig.ctx)
    if how == "kill":
        rig.pool.stop(rig.ctx, "kill")
        assert any("stops once the gates end" in m for _, m in rig.ctx.events)
    else:
        rig.now[0] += 10 ** 7
    for _ in range(5):
        assert rig.pool.collect(rig.ctx) == []
    assert not [c for c in rig.ctl.calls if c[0] == "cancel"]
    release.set()                                   # the check ends unsettled: cancel now
    (out,) = _collect_until(rig)
    assert [c[0] for c in rig.ctl.calls if c[0] == "cancel"] == ["cancel"]
    assert out["outcome"] == "resume" and out["requeue"]


def test_a_kill_during_a_passing_check_keeps_the_verified_result(tmp_path):
    rig = Rig(tmp_path, background=True)
    aid = rig.start("1")[0]["attempt_id"]
    _, release = _slow_verify(rig, aid)
    rig.ctl.polls[aid] = S.VERIFYING
    rig.pool.collect(rig.ctx)
    rig.pool.stop(rig.ctx, "kill")
    release.set()
    (out,) = _collect_until(rig)
    assert out["outcome"] == "merge"
    assert not [c for c in rig.ctl.calls if c[0] == "cancel"]


def test_a_crashing_check_is_reported_and_retried(tmp_path, monkeypatch):
    rig = Rig(tmp_path, background=True)
    aid = rig.start("1")[0]["attempt_id"]
    calls = []

    def boom(ctx, hr):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("worker died")
        return hybrid.HybridPool._verify(rig.pool, ctx, hr)
    monkeypatch.setattr(rig.pool, "_verify", boom)
    rig.ctl.polls[aid] = S.VERIFYING
    (out,) = _collect_until(rig)
    assert any("verification crashed: RuntimeError" in m for _, m in rig.ctx.events)
    assert out["outcome"] == "merge" and len(calls) == 2
