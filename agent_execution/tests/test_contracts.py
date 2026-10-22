"""Contract acceptance and adversarial wire-format fixtures; no provider calls."""
from dataclasses import FrozenInstanceError, replace
import json
import unittest

from agent_execution.adapter import (
    AgentAdapter, Cancellation, PreparedExecution, ProcessIdentity, Recovery,
    require_agent, require_prepared, require_result,
)
from agent_execution.contracts import (
    AgentSpec, ArtifactRef, Attempt, AttemptState as S, Boundary, Budget, Claim,
    ContractError, ExecutionEvent, ExecutionPolicy, ExecutionResult, Handoff,
    Privacy, Repository, ResultStatus, TaskSpec, Usage,
)
from agent_execution.lifecycle import transition
from agent_execution.tests.fixtures import ARTIFACT, CLAIM, SHA, FakeAdapter, agent, attempt, handoff, task


class WireContracts(unittest.TestCase):
    def test_required_contracts_roundtrip_without_losing_fields(self):
        fake = FakeAdapter()
        fake.terminated = True
        objects = (task(), agent(), attempt(), handoff(), fake.collect(attempt()),
                   next(iter(fake.events(attempt()))), Cancellation(requested=True,
                   termination_confirmed=False, reason="operator"), Recovery(status="missing"))
        for obj in objects:
            with self.subTest(contract=type(obj).__name__):
                encoded = obj.to_json()
                restored = type(obj).from_json(encoded)
                self.assertEqual(restored, obj)
                self.assertEqual(restored.to_dict(), json.loads(encoded))

    def test_unknown_usage_stays_null(self):
        usage = Usage.from_json(Usage().to_json())
        self.assertIsNone(usage.tokens)
        self.assertIsNone(usage.cost_amount)
        self.assertEqual(usage.cost_measurement, "unknown")

    def test_invalid_wire_data_is_refused(self):
        for patch in ({"schema_version": 2}, {"schema_version": True}, {"unexpected": "field"},
                      {"objective": " "}, {"required_capabilities": "repository_edit"},
                      {"requirements": []}, {"acceptance_criteria": []}, {"priority": "guess"},
                      {"dependencies": ["openproject:fixture"]}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                TaskSpec.from_dict({**task().to_dict(), **patch})

    def test_missing_version_and_required_fields_are_refused(self):
        for key in ("schema_version", "task_id", "repository", "execution_policy", "budget"):
            data = task().to_dict()
            del data[key]
            with self.subTest(key=key), self.assertRaises(ContractError):
                TaskSpec.from_dict(data)

    def test_nested_unknown_fields_are_refused(self):
        data = task().to_dict()
        data["execution_policy"]["approve_everything"] = True
        with self.assertRaises(ContractError):
            TaskSpec.from_dict(data)

    def test_duplicate_json_keys_are_refused(self):
        with self.assertRaises(ContractError):
            Usage.from_json('{"schema_version":1,"schema_version":2}')

    def test_boolean_and_nonfinite_numeric_values_are_refused(self):
        for patch in ({"tokens": True}, {"tokens": 1.5}, {"tokens": -1}, {"wall_seconds": True},
                      {"wall_seconds": float("nan")}, {"vram_peak_mib": -2},
                      {"cost_amount": float("inf")}, {"cost_amount": 10 ** 500}, {"cost_amount": 0}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                Usage(**patch)

    def test_known_cost_requires_measurement_and_basis(self):
        with self.assertRaises(ContractError):
            Usage(cost_measurement="measured", cost_amount=0)
        usage = Usage(cost_measurement="estimated", cost_amount=0, cost_basis="subscription-notional")
        self.assertEqual(usage.cost_amount, 0.0)

    def test_unknown_budget_is_not_unlimited(self):
        for kwargs in ({"wall_seconds": 1}, {"wall_seconds": True},
                       {"wall_seconds": 0, "cost_limit": 1}, {"wall_seconds": 1, "cost_limit": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                Budget(**kwargs)

    def test_full_commit_and_safe_absolute_repository_required(self):
        for patch in ({"base_commit": "abc"}, {"path": "relative"}, {"path": "/tmp/../other"},
                      {"path": "/tmp/bad\x00path"}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                replace(task().repository, **patch)

    def test_artifact_digest_required(self):
        with self.assertRaises(ContractError):
            ArtifactRef(ref="artifact:bad", digest="guess")

    def test_duplicate_or_conflicting_actions_refused(self):
        policy = task().execution_policy
        for patch in ({"allowed_actions": ("read", "read")}, {"approval_actions": ("read",)},
                      {"max_attempts": 0}, {"max_attempts": True}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                replace(policy, **patch)

    def test_handoff_is_frozen_and_preserves_pending_decisions_failures(self):
        value = Handoff.from_json(handoff().to_json())
        self.assertEqual(value.known_failures, ("Implementation not started",))
        self.assertEqual(value.decisions[0].reason, "Required")
        with self.assertRaises(FrozenInstanceError):
            value.pending = ()

    def test_incomplete_workspace_handoff_refused(self):
        for patch in ({"workspace_status": "explicit-diff"}, {"workspace_status": "committed"},
                      {"instruction_refs": ()}, {"next_actions": ()}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                replace(handoff(), **patch)

    def test_claim_requires_utc_positive_generation(self):
        for patch in ({"generation": 0}, {"generation": True}, {"lease_expires_at": "tomorrow"},
                      {"lease_expires_at": "2026-10-20T12:00:00"},
                      {"lease_expires_at": "2026-10-20T12:00:00+02:00"}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                replace(CLAIM, **patch)

    def test_attempt_requires_complete_owner_and_process_identity(self):
        for patch in ({"claim": None}, {"process_pid": 12}, {"process_start": "start"},
                      {"attempt_number": 0}, {"retry_of": "attempt-1"}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                replace(attempt(), **patch)

    def test_event_rejects_untrusted_python_payload_and_copies_input(self):
        original = {"nested": ["safe"]}
        event = ExecutionEvent(execution_id="e", attempt_id="a", generation=1, sequence=1,
                               type="progress", timestamp="2026-10-20T12:00:00Z", payload=original)
        original["nested"].append("changed")
        self.assertEqual(event.payload, {"nested": ["safe"]})
        for payload in ({"x": object()}, {"x": float("nan")}, {1: "non-string"}):
            with self.subTest(payload=payload), self.assertRaises(ContractError):
                replace(event, payload=payload)
        for patch in ({"type": "execute-arbitrary-command"}, {"sequence": 0}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                replace(event, **patch)

    def test_result_cannot_claim_controller_success_or_unexplained_failure(self):
        fake = FakeAdapter()
        fake.terminated = True
        result = fake.collect(attempt())
        for patch in ({"status": "succeeded"}, {"status": "failed"},
                      {"changes": ("module.py",)}, {"changes": ("../escape",), "diff_ref": ARTIFACT}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                replace(result, **patch)


class AdapterFixtures(unittest.TestCase):
    def test_synthetic_backend_profiles_satisfy_protocol_and_normalize_outcomes(self):
        for name in ("claude", "codex", "local"):
            spec = task()
            if name != "local":
                spec = replace(spec, execution_policy=replace(spec.execution_policy, privacy=Privacy.CLOUD_ALLOWED))
            for outcome in ResultStatus:
                with self.subTest(backend=name, outcome=outcome):
                    fake = FakeAdapter(name, outcome)
                    self.assertIsInstance(fake, AgentAdapter)
                    run = attempt(name)
                    plan = fake.prepare(spec, ARTIFACT)
                    identity = fake.start(run, plan)
                    self.assertGreater(identity.pid, 0)
                    self.assertEqual(fake.recover(run).status, "adopted")
                    fake.terminated = True
                    result = fake.collect(run)
                    require_result(run, result)
                    self.assertEqual(result.status, outcome)
                    self.assertIsNone(result.usage.cost_amount)
                    self.assertEqual(fake.recover(run).status, "exited")

    def test_local_only_cannot_fallback_to_cloud(self):
        for name in ("claude", "codex"):
            with self.subTest(name=name), self.assertRaises(ContractError):
                FakeAdapter(name).prepare(task(), ARTIFACT)

    def test_claimed_capability_is_not_validated_capability(self):
        with self.assertRaises(ContractError):
            require_agent(task(), replace(agent(), validated_capabilities=()))
        with self.assertRaises(ContractError):
            replace(agent(), validation_ref=None)
        with self.assertRaises(ContractError):
            replace(agent(), agent_type="inference_server")
        with self.assertRaises(ContractError):
            replace(agent("codex"), privacy_compatibility=(Privacy.LOCAL_ONLY,))

    def test_unknown_availability_is_not_eligible(self):
        with self.assertRaises(ContractError):
            require_agent(task(), replace(agent(), availability="unknown"))

    def test_prepared_scope_cannot_be_reused_for_another_context(self):
        plan = FakeAdapter().prepare(task(), ARTIFACT)
        for patch in ({"task_revision": "r2"}, {"agent_id": "other"}, {"policy_revision": "p2"},
                      {"context_ref": replace(ARTIFACT, digest="c" * 64)}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                require_prepared(replace(attempt(), **patch), plan)

    def test_result_correlation_refuses_stale_or_foreign_results(self):
        fake = FakeAdapter()
        fake.terminated = True
        result = fake.collect(attempt())
        for patch in ({"generation": 8}, {"attempt_id": "other"}, {"execution_id": "other"},
                      {"task_id": "other"}, {"agent_id": "other"}, {"base_commit": "c" * 40}):
            with self.subTest(patch=patch), self.assertRaises(ContractError):
                require_result(attempt(), replace(result, **patch))

    def test_cursor_resume_does_not_replay_earlier_events(self):
        self.assertEqual([event.sequence for event in FakeAdapter().events(attempt(), cursor=1)], [2])

    def test_cancellation_request_does_not_release_workspace(self):
        fake = FakeAdapter()
        self.assertFalse(fake.cancel(attempt(), "operator").workspace_releasable)
        with self.assertRaises(ContractError):
            fake.collect(attempt())
        fake.terminated = True
        self.assertTrue(fake.cancel(attempt(), "operator").workspace_releasable)


class LifecycleGuards(unittest.TestCase):
    def advance(self, run, state, **kwargs):
        return transition(run, state, owner_id=CLAIM.owner_id, generation=CLAIM.generation, **kwargs)

    def test_claim_is_explicit_and_then_execution_progresses(self):
        queued = replace(attempt(), state=S.QUEUED, claim=None)
        claimed = self.advance(queued, S.CLAIMED, new_claim=CLAIM)
        self.assertEqual(claimed.claim, CLAIM)
        self.assertEqual(self.advance(claimed, S.RUNNING).state, S.RUNNING)
        with self.assertRaises(ContractError):
            self.advance(queued, S.CLAIMED)

    def test_stale_or_missing_owner_refused(self):
        for identity in ({}, {"owner_id": "other", "generation": 7},
                         {"owner_id": CLAIM.owner_id, "generation": 6},
                         {"owner_id": CLAIM.owner_id, "generation": True}):
            with self.subTest(identity=identity), self.assertRaises(ContractError):
                transition(attempt(), S.RECOVERING, **identity)

    def test_success_requires_termination_then_independent_verification(self):
        with self.assertRaises(ContractError):
            self.advance(attempt(), S.VERIFYING)
        verifying = self.advance(attempt(), S.VERIFYING, termination_confirmed=True)
        with self.assertRaises(ContractError):
            self.advance(verifying, S.SUCCEEDED, termination_confirmed=True)
        done = self.advance(verifying, S.SUCCEEDED, termination_confirmed=True, verification_passed=True)
        self.assertEqual(done.state, S.SUCCEEDED)
        self.assertEqual(attempt().state, S.RUNNING)
        with self.assertRaises(ContractError):
            self.advance(done, S.RUNNING)

    def test_expired_lease_does_not_confirm_termination(self):
        expired = replace(attempt(), claim=replace(CLAIM, lease_expires_at="2000-01-01T00:00:00Z"))
        cancelling = self.advance(expired, S.CANCELLING)
        with self.assertRaises(ContractError):
            self.advance(cancelling, S.CANCELLED)
        self.assertEqual(self.advance(cancelling, S.CANCELLED, termination_confirmed=True).state, S.CANCELLED)

    def test_handoff_and_approval_require_their_evidence(self):
        with self.assertRaises(ContractError):
            self.advance(attempt(), S.HANDOFF_READY, termination_confirmed=True)
        checkpoint = self.advance(attempt(), S.HANDOFF_READY, termination_confirmed=True, checkpoint_accepted=True)
        with self.assertRaises(ContractError):
            self.advance(checkpoint, S.QUEUED)
        waiting = self.advance(attempt(), S.AWAITING_APPROVAL)
        with self.assertRaises(ContractError):
            self.advance(waiting, S.RUNNING)
        self.assertEqual(self.advance(waiting, S.RUNNING, approval_confirmed=True).state, S.RUNNING)

    def test_transition_cannot_replace_claim_or_coerce_evidence(self):
        with self.assertRaises(ContractError):
            self.advance(attempt(), S.RECOVERING, new_claim=replace(CLAIM, generation=8))
        with self.assertRaises(ContractError):
            self.advance(attempt(), S.VERIFYING, termination_confirmed="yes")


if __name__ == "__main__":
    unittest.main()
