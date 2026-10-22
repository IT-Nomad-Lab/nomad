"""Independent SQLite connections, restart persistence and fenced completion fixtures."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from agent_execution.contracts import AttemptState as S, CheckResult, ContractError, ExecutionResult, ResultStatus, Usage
from agent_execution.store import AttemptStore
from agent_execution.tests.fixtures import ARTIFACT, SHA, task


class DurableAttempts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = [1000.0]
        self.task = replace(task(), execution_policy=replace(task().execution_policy, max_attempts=2))
        self.store = self.open()

    def open(self):
        store = AttemptStore(self.root / 'attempts.db', clock=lambda: self.now[0])
        self.addCleanup(store.close)
        return store

    def claim(self, store=None, **kwargs):
        return (store or self.store).claim(self.task, ARTIFACT, agent_id='local',
                                          workspace=str(self.root / 'workspace'), owner_id='scheduler', **kwargs)

    def advance(self, run, state, **kwargs):
        return self.store.advance(run.attempt_id, run.claim.owner_id, run.claim.generation, state, **kwargs)

    def running(self):
        run = self.claim()
        return self.store.record_process(run.attempt_id, run.claim.owner_id, run.claim.generation,
                                         pid=12345, start_time='fixture-process-start')

    def result(self, run):
        return ExecutionResult(execution_id=run.execution_id, attempt_id=run.attempt_id,
                               task_id=run.task_id, agent_id=run.agent_id, generation=run.claim.generation,
                               status=ResultStatus.CANDIDATE_COMPLETE, workspace=run.workspace,
                               base_commit=run.base_commit, result_commit=SHA, usage=Usage(),
                               test_results=(CheckResult(gate_id='G1', status='passed', evidence=ARTIFACT),))

    def test_independent_schedulers_contend_and_exactly_one_claims(self):
        second = self.open()
        barrier = threading.Barrier(2)
        def contender(store):
            barrier.wait()
            try:
                return self.claim(store)
            except ContractError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(contender, (self.store, second)))
        self.assertEqual(sum(run is not None for run in runs), 1)

    def test_same_workspace_cannot_be_given_to_a_different_task(self):
        self.claim()
        other = replace(self.task, task_id='openproject:other')
        with self.assertRaises(ContractError):
            self.store.claim(other, ARTIFACT, agent_id='local', workspace=str(self.root / 'workspace'), owner_id='other')
        with self.assertRaises(ContractError):
            self.store.claim(other, ARTIFACT, agent_id='local', workspace=str(self.root / 'workspace/sub'), owner_id='other')
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_expiry_and_restart_never_release_uncertain_workspace(self):
        run = self.running()
        self.now[0] += 61
        reopened = self.open()
        self.assertEqual(reopened.get(run.attempt_id).process_pid, 12345)
        with self.assertRaises(ContractError):
            self.claim(reopened, retry_authorized=True)
        with self.assertRaises(ContractError):
            reopened.heartbeat(run.attempt_id, 'scheduler', run.claim.generation)
        with self.assertRaises(ContractError):
            reopened.reconcile_termination(run.attempt_id, 'scheduler', run.claim.generation,
                                           termination_confirmed=False)
        reopened.reconcile_termination(run.attempt_id, 'scheduler', run.claim.generation,
                                       termination_confirmed=True)
        replacement = self.claim(reopened, retry_authorized=True)
        self.assertNotEqual(replacement.attempt_id, run.attempt_id)
        self.assertGreater(replacement.claim.generation, run.claim.generation)
        self.assertEqual(replacement.attempt_number, 2)

    def test_retry_limit_survives_restart_and_old_generation_is_rejected(self):
        first = self.claim()
        self.store.reconcile_termination(first.attempt_id, 'scheduler', first.claim.generation, termination_confirmed=True)
        with self.assertRaises(ContractError):
            self.claim()
        second = self.claim(retry_authorized=True)
        with self.assertRaises(ContractError):
            self.store.accept_result(first.attempt_id, 'scheduler', first.claim.generation, self.result(first))
        self.store.reconcile_termination(second.attempt_id, 'scheduler', second.claim.generation, termination_confirmed=True)
        with self.assertRaises(ContractError):
            self.claim(self.open(), retry_authorized=True)
        self.assertEqual(self.store.get(first.attempt_id).state, S.BLOCKED)

    def test_task_revision_does_not_bypass_an_active_reservation(self):
        self.claim()
        changed = replace(self.task, task_revision='r2')
        with self.assertRaises(ContractError):
            self.store.claim(changed, ARTIFACT, agent_id='local', workspace=str(self.root / 'other'), owner_id='other')

    def test_heartbeat_renews_only_the_current_live_claim(self):
        run = self.claim()
        self.now[0] += 10
        renewed = self.store.heartbeat(run.attempt_id, 'scheduler', run.claim.generation)
        self.assertNotEqual(renewed.claim.lease_expires_at, run.claim.lease_expires_at)
        with self.assertRaises(ContractError):
            self.store.heartbeat(run.attempt_id, 'other', run.claim.generation)
        with self.assertRaises(ContractError):
            self.store.heartbeat(run.attempt_id, 'scheduler', True)

    def test_process_cannot_be_registered_twice(self):
        run = self.running()
        with self.assertRaises(ContractError):
            self.store.record_process(run.attempt_id, 'scheduler', run.claim.generation, pid=23456, start_time='other')

    def test_cancellation_does_not_release_until_termination_confirmed(self):
        run = self.running()
        self.advance(run, S.CANCELLING)
        with self.assertRaises(ContractError):
            self.advance(run, S.CANCELLED)
        with self.assertRaises(ContractError):
            self.claim(retry_authorized=True)
        self.advance(run, S.CANCELLED, termination_confirmed=True)
        self.assertEqual(self.claim(retry_authorized=True).attempt_number, 2)

    def test_result_requires_matching_scope_and_current_generation(self):
        run = self.running()
        for result in (replace(self.result(run), generation=run.claim.generation + 1),
                       replace(self.result(run), attempt_id='foreign')):
            with self.subTest(result=result), self.assertRaises(ContractError):
                self.store.accept_result(run.attempt_id, 'scheduler', run.claim.generation, result)
        self.store.accept_result(run.attempt_id, 'scheduler', run.claim.generation, self.result(run))

    def test_success_and_tracker_effect_commit_atomically_and_delivery_is_idempotent(self):
        run = self.running()
        self.advance(run, S.VERIFYING, termination_confirmed=True)
        with self.assertRaises(TypeError):
            self.store.complete(run.attempt_id, 'scheduler', run.claim.generation, self.result(run),
                                verification_ref=ARTIFACT, tracker_payload={'invalid': object()})
        self.assertEqual(self.store.get(run.attempt_id).state, S.VERIFYING)
        self.assertEqual(self.store.pending_effects(), [])
        done = self.store.complete(run.attempt_id, 'scheduler', run.claim.generation, self.result(run),
                                   verification_ref=ARTIFACT, tracker_payload={'state': 'Done'})
        self.assertEqual(done.state, S.SUCCEEDED)
        reopened = self.open()
        effects = reopened.pending_effects()
        self.assertEqual(len(effects), 1)
        effect = effects[0]['effect_id']
        reopened.note_delivery(effect, error='tracker unavailable')
        self.assertEqual(reopened.pending_effects()[0]['attempts'], 1)
        reopened.note_delivery(effect)
        reopened.note_delivery(effect)
        self.assertEqual(reopened.pending_effects(), [])
        with self.assertRaises(ContractError):
            self.claim(reopened, retry_authorized=True)

    def test_failed_missing_or_uncommitted_verification_cannot_complete(self):
        run = self.running()
        self.advance(run, S.VERIFYING, termination_confirmed=True)
        for result in (replace(self.result(run), test_results=()),
                       replace(self.result(run), test_results=(CheckResult(gate_id='G1', status='failed', evidence=ARTIFACT),)),
                       replace(self.result(run), result_commit=None)):
            with self.subTest(result=result), self.assertRaises(ContractError):
                self.store.complete(run.attempt_id, 'scheduler', run.claim.generation, result,
                                    verification_ref=ARTIFACT, tracker_payload={'state': 'Done'})
        with self.assertRaises(ContractError):
            self.advance(run, S.SUCCEEDED, termination_confirmed=True, verification_passed=True)

    def test_approval_consumption_survives_restart_and_cannot_be_reset(self):
        run = self.running()
        self.store.register_approval('approval-1', task_id=run.task_id, scope_digest=ARTIFACT.digest,
                                     max_uses=1, expires_at=self.now[0] + 100)
        self.store.consume_approval('approval-1', run.attempt_id, 'scheduler', run.claim.generation,
                                    scope_digest=ARTIFACT.digest)
        reopened = self.open()
        reopened.register_approval('approval-1', task_id=run.task_id, scope_digest=ARTIFACT.digest,
                                    max_uses=1, expires_at=self.now[0] + 100)
        with self.assertRaises(ContractError):
            reopened.consume_approval('approval-1', run.attempt_id, 'scheduler', run.claim.generation,
                                       scope_digest=ARTIFACT.digest)
        with self.assertRaises(ContractError):
            reopened.register_approval('approval-1', task_id=run.task_id, scope_digest='c' * 64,
                                        max_uses=2, expires_at=self.now[0] + 100)

    def test_verification_claim_survives_reopen_and_refuses_second_runner(self):
        run = self.running()
        self.store.accept_result(run.attempt_id, 'scheduler', run.claim.generation, self.result(run))
        self.advance(run, S.VERIFYING, termination_confirmed=True)
        self.store.begin_verification(run.attempt_id, 'scheduler', run.claim.generation)
        reopened = self.open()
        with self.assertRaisesRegex(ContractError, 'unreconciled claim'):
            reopened.begin_verification(run.attempt_id, 'scheduler', run.claim.generation)

    def test_version_one_migration_preserves_attempt_and_adds_verifier_fencing(self):
        run = self.running()
        self.store.close()
        previous = sqlite3.connect(self.root / 'attempts.db')
        previous.execute('DROP TABLE verification_claims')
        previous.execute('PRAGMA user_version=1')
        previous.commit()
        previous.close()
        migrated = self.open()
        self.assertEqual(migrated.get(run.attempt_id), run)
        self.assertEqual(migrated.db.execute('PRAGMA user_version').fetchone()[0], 3)
        migrated.accept_result(run.attempt_id, 'scheduler', run.claim.generation, self.result(run))
        migrated.advance(run.attempt_id, 'scheduler', run.claim.generation, S.VERIFYING, termination_confirmed=True)
        self.assertTrue(migrated.begin_verification(run.attempt_id, 'scheduler', run.claim.generation))

    def test_unknown_database_schema_refused(self):
        path = self.root / 'future.db'
        db = sqlite3.connect(path)
        db.execute('PRAGMA user_version=99')
        db.close()
        with self.assertRaises(ContractError):
            AttemptStore(path)

    def test_outbox_identity_cannot_be_rebound(self):
        self.store.enqueue_effect('effect-1', self.task.task_id, {'status': 'Done'})
        self.store.enqueue_effect('effect-1', self.task.task_id, {'status': 'Done'})
        self.assertEqual(len(self.store.pending_effects()), 1)
        with self.assertRaises(ContractError):
            self.store.enqueue_effect('effect-1', self.task.task_id, {'status': 'Blocked'})


if __name__ == '__main__':
    unittest.main()
