"""Controller wiring for dirty candidates: finalize the exact snapshot, then verify that commit."""
from dataclasses import replace
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from agent_execution.contracts import AttemptState as S, ContractError, Privacy, ResultStatus
from agent_execution.controller import ExecutionController
from agent_execution.finalization import CandidateFinalizer
from agent_execution.native import NativeAdapter, NativeProfile
from agent_execution.policy import EffectivePolicy
from agent_execution.tests import test_native as native_fixtures
from agent_execution.tests.fixtures import agent, task
from agent_execution.verification import CommitVerifier, Gate

AUTHOR = ('Nomad Finalizer', 'finalizer@example.invalid')
GATE = "from pathlib import Path; assert Path('module.py').read_text() == 'VALUE = 1\\n'"


class Harness(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeExecution('test_codex_consumes_full_context_and_normalizes_unknown_cost')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.task = replace(task(), repository=replace(task().repository, path=str(f.repo), base_commit=f.base),
                            execution_policy=replace(task().execution_policy, privacy=Privacy.CLOUD_ALLOWED))
        profile = NativeProfile(backend='codex', agent=agent('codex'),
                                command_prefix=(sys.executable, str(f.script)), settings_args=(),
                                policy_revision='p1', tool_profile_ref='tools:offline',
                                environment={'FIXTURE_BACKEND': 'codex', 'FIXTURE_MODE': 'write',
                                             'FIXTURE_CAPTURE': str(f.root / 'captured.json')})
        self.adapter = NativeAdapter(profile, f.artifacts, f.store, f.root / 'runtime')
        self.authorize = Mock()
        self.scope = Mock(return_value=('feat/fixture', ('module.py',)))
        self.finalizer = CandidateFinalizer(f.artifacts, AUTHOR, commit_date='2026-01-01T00:00:00Z')
        self.controller = self.make()
        self.addCleanup(lambda: self.controller.close())

    def make(self, **overrides):
        kwargs = dict(authorize=self.authorize, finalizer=self.finalizer, finalization_scope=self.scope)
        kwargs.update(overrides)
        return ExecutionController(self.fixture.store, self.fixture.artifacts, {'codex': self.adapter}, **kwargs)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.fixture.worktree), *args], text=True).strip()

    def candidate(self):
        run = self.controller.start(self.task, 'codex', self.fixture.instructions,
                                    workspace=str(self.fixture.worktree))
        self.fixture.adapters.append((self.adapter, run))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.controller.poll(run.attempt_id)
            if result is not None:
                break
            time.sleep(0.02)
        else:
            self.fail('fixture agent did not finish')
        self.assertEqual(result.status, ResultStatus.CANDIDATE_COMPLETE)
        self.assertIsNone(result.result_commit)
        self.assertIsNotNone(result.diff_ref)
        self.assertEqual(result.changes, ('module.py',))
        return run

    def verifier(self):
        return CommitVerifier(self.fixture.artifacts, self.fixture.root / 'verification',
                              (Gate('G1', ('/usr/bin/python3', '-c', GATE), 5),))

    def assert_untouched(self, run):
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.fixture.base)
        self.assertEqual(self.git('status', '--porcelain'), '?? module.py')
        self.assertIsNone(self.fixture.store.result(run.attempt_id).result_commit)
        self.assertEqual(self.fixture.store.pending_effects(), [])
        self.assertIsNone(self.fixture.store.verification_claim(run.attempt_id))


class DirtyCandidateController(Harness):
    def test_dirty_candidate_is_finalized_then_the_exact_commit_is_verified(self):
        run = self.candidate()
        done = self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(done.state, S.SUCCEEDED)
        head = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(head, self.fixture.base)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), self.fixture.base)
        self.assertIn(f'Execution-Attempt: {run.attempt_id}', self.git('log', '-1', '--format=%B'))
        self.assertEqual(self.git('status', '--porcelain'), '')
        stored = self.fixture.store.result(run.attempt_id)
        self.assertEqual((stored.result_commit, stored.diff_ref), (head, None))
        self.assertEqual(stored.test_results[0].status, 'passed')
        (effect,) = self.fixture.store.pending_effects()
        self.assertIn(head, effect['payload'])
        phases = [c.args[2] for c in self.authorize.call_args_list]
        self.assertLess(phases.index('finalize'), len(phases) - 1)
        self.assertEqual(phases[-1], 'verify')
        self.scope.assert_called_once()
        self.assertIs(self.scope.call_args.args[0], self.task)

    def test_without_a_finalizer_a_dirty_candidate_is_refused_and_left_reserved(self):
        self.controller.close()
        self.controller = self.make(finalizer=None, finalization_scope=None)
        run = self.candidate()
        with self.assertRaisesRegex(ContractError, 'requires a configured finalizer'):
            self.controller.verify(run.attempt_id, self.verifier())
        self.assert_untouched(run)

    def test_changes_outside_the_trusted_scope_are_refused_without_a_commit(self):
        self.scope.return_value = ('feat/fixture', ('src',))
        run = self.candidate()
        with self.assertRaisesRegex(ContractError, 'outside the allowed paths'):
            self.controller.verify(run.attempt_id, self.verifier())
        self.assert_untouched(run)

    def test_wrong_branch_from_the_scope_is_refused_without_a_commit(self):
        self.scope.return_value = ('feat/other', ('module.py',))
        run = self.candidate()
        with self.assertRaisesRegex(ContractError, 'feature branch'):
            self.controller.verify(run.attempt_id, self.verifier())
        self.assert_untouched(run)

    def test_policy_revoked_at_finalization_refuses_without_a_commit(self):
        run = self.candidate()
        self.authorize.side_effect = lambda task, agent, phase: (
            (_ for _ in ()).throw(ContractError('revoked')) if phase == 'finalize' else None)
        with self.assertRaisesRegex(ContractError, 'policy re-check failed'):
            self.controller.verify(run.attempt_id, self.verifier())
        self.assert_untouched(run)

    def test_unproven_termination_refuses_without_a_commit(self):
        run = self.candidate()
        with patch.object(self.adapter, 'recover', return_value=Mock(status='adopted')):
            with self.assertRaisesRegex(ContractError, 'termination is not proven'):
                self.controller.verify(run.attempt_id, self.verifier())
        self.assert_untouched(run)

    def test_crash_after_the_commit_recovers_the_same_commit_on_retry(self):
        run = self.candidate()
        with patch.object(self.fixture.store, 'record_finalized', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.controller.verify(run.attempt_id, self.verifier())
        first = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(first, self.fixture.base)
        self.assertIsNone(self.fixture.store.result(run.attempt_id).result_commit)
        self.assertEqual(self.fixture.store.pending_effects(), [])
        done = self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(done.state, S.SUCCEEDED)
        self.assertEqual(self.git('rev-parse', 'HEAD'), first)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(self.fixture.store.result(run.attempt_id).result_commit, first)

    def test_a_failing_gate_on_the_finalized_commit_blocks_without_done(self):
        run = self.candidate()
        failing = CommitVerifier(self.fixture.artifacts, self.fixture.root / 'verification',
                                 (Gate('G1', ('/usr/bin/python3', '-c', 'raise SystemExit(1)'), 5),))
        with self.assertRaisesRegex(ContractError, 'independent gate failed'):
            self.controller.verify(run.attempt_id, failing)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.BLOCKED)
        (effect,) = self.fixture.store.pending_effects()
        self.assertNotIn('"Done"', effect['payload'])

    def test_finalizer_and_scope_must_be_configured_together(self):
        self.controller.close()
        for kwargs in ({'finalizer': self.finalizer, 'finalization_scope': None},
                       {'finalizer': None, 'finalization_scope': self.scope},
                       {'finalizer': self.finalizer, 'finalization_scope': ('feat/fixture', ('module.py',))}):
            with self.assertRaises(ContractError):
                self.make(**kwargs)


class StoreFinalizationFence(Harness):
    """record_finalized only swaps a stored dirty candidate for its own commit."""

    def committed(self, run):
        stored = self.fixture.store.result(run.attempt_id)
        return stored, replace(stored, result_commit=self.fixture.base, diff_ref=None)

    def owner(self, run):
        current = self.fixture.store.get(run.attempt_id)
        return run.attempt_id, self.controller.owner_id, current.claim.generation

    def test_store_accepts_only_the_same_candidate_with_a_commit(self):
        run = self.candidate()
        stored, good = self.committed(run)
        for bad in (replace(good, changes=('other.py',)),
                    replace(good, diff_ref=stored.diff_ref), replace(good, errors=('x',)),
                    stored):
            with self.assertRaises(ContractError):
                self.fixture.store.record_finalized(*self.owner(run), bad)
        self.assertIsNone(self.fixture.store.result(run.attempt_id).result_commit)
        self.assertEqual(self.fixture.store.record_finalized(*self.owner(run), good), good)
        with self.assertRaisesRegex(ContractError, 'only a stored dirty candidate'):
            self.fixture.store.record_finalized(*self.owner(run), good)

    def test_store_refuses_stale_owner_and_held_verifier_claim(self):
        run = self.candidate()
        _, good = self.committed(run)
        attempt_id, owner, generation = self.owner(run)
        with self.assertRaises(ContractError):
            self.fixture.store.record_finalized(attempt_id, owner, generation + 1, good)
        with self.assertRaises(ContractError):
            self.fixture.store.record_finalized(attempt_id, 'someone-else', generation, good)
        self.fixture.store.begin_verification(attempt_id, owner, generation)
        with self.assertRaisesRegex(ContractError, 'verifier claim'):
            self.fixture.store.record_finalized(attempt_id, owner, generation, good)

    def test_store_refuses_once_the_attempt_left_verification(self):
        run = self.candidate()
        _, good = self.committed(run)
        attempt_id, owner, generation = self.owner(run)
        self.fixture.store.request_cancellation(attempt_id, owner, generation)
        with self.assertRaisesRegex(ContractError, 'not awaiting verification'):
            self.fixture.store.record_finalized(*self.owner(run), good)
        self.assertIsNone(self.fixture.store.result(attempt_id).result_commit)


class FinalizePhase(unittest.TestCase):
    def test_effective_policy_knows_the_finalize_phase_and_still_refuses_unknown_ones(self):
        current = Mock(return_value=None)
        policy = EffectivePolicy(current_task=current, profiles=(), operators=(), store=None)
        with self.assertRaisesRegex(ContractError, 'changed; reconciliation required'):
            policy(task(), agent('codex'), 'finalize')
        with self.assertRaisesRegex(ContractError, 'unknown policy lifecycle phase'):
            policy(task(), agent('codex'), 'commit')
        current.assert_called_once()


if __name__ == '__main__':
    unittest.main()
