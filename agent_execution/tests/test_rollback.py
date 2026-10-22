"""Staged rollback: durable dispatch gate, writer inventory and refused legacy handback."""
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from agent_execution.contracts import AttemptState as S, ContractError
from agent_execution.rollback import RollbackCoordinator
from agent_execution.tests.fixtures import ARTIFACT, task
from agent_execution.tests import test_store


class StagedRollback(unittest.TestCase):
    # Reuse the store fixture helpers without re-collecting DurableAttempts' own tests.
    _helpers = test_store.DurableAttempts
    open, claim, advance, running, result = (_helpers.open, _helpers.claim, _helpers.advance,
                                             _helpers.running, _helpers.result)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = [1000.0]
        self.task = replace(task(), execution_policy=replace(task().execution_policy, max_attempts=2))
        self.store = self.open()
        self.rollback = RollbackCoordinator(self.store)

    def test_dispatch_disable_survives_restart_and_prevents_launch(self):
        self.rollback.begin(actor='operator', reason='rollback drill')
        self.rollback.begin(actor='operator', reason='rollback drill')
        reopened = self.open()
        self.assertTrue(reopened.dispatch_disabled())
        with self.assertRaisesRegex(ContractError, 'dispatch is disabled'):
            self.claim(reopened)
        self.assertEqual(reopened.rollback_inventory()['unterminated'], [])
        RollbackCoordinator(reopened).resume(actor='operator', reason='drill finished')
        self.assertFalse(self.open().dispatch_disabled())
        self.assertEqual(self.claim(reopened).attempt_number, 1)

    def test_dispatch_change_requires_actor_and_reason(self):
        for kwargs in ({'actor': '', 'reason': 'x'}, {'actor': 'operator', 'reason': ''}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                self.rollback.begin(**kwargs)
        with self.assertRaises(ContractError):
            self.store.set_dispatch(enabled=0, actor='operator', reason='x')
        self.assertFalse(self.store.dispatch_disabled())

    def test_handback_requires_dispatch_disabled_first(self):
        with self.assertRaisesRegex(ContractError, 'still enabled'):
            self.rollback.handback()

    def test_uncertain_native_writer_blocks_legacy_handback(self):
        run = self.running()
        self.advance(run, S.CANCELLING)
        self.rollback.begin(actor='operator', reason='rollback')
        with self.assertRaisesRegex(ContractError, 'not proven terminated') as raised:
            self.rollback.handback()
        self.assertIn('still reserved', str(raised.exception))
        self.assertEqual(self.store.get(run.attempt_id).state, S.CANCELLING)

    def test_live_or_uncertain_verifier_blocks_legacy_handback(self):
        run = self.running()
        self.store.accept_result(run.attempt_id, 'scheduler', run.claim.generation, self.result(run))
        self.advance(run, S.VERIFYING, termination_confirmed=True)
        self.store.begin_verification(run.attempt_id, 'scheduler', run.claim.generation)
        self.rollback.begin(actor='operator', reason='rollback')
        with self.assertRaisesRegex(ContractError, 'verifier for .* not reconciled'):
            self.rollback.handback()

    def test_reconciled_attempts_allow_single_legacy_owner_and_keep_pending_effects(self):
        run = self.running()
        self.advance(run, S.CANCELLING)
        self.rollback.begin(actor='operator', reason='rollback')
        self.store.finish_cancellation(run.attempt_id, 'scheduler', run.claim.generation,
                                       termination_confirmed=True, tracker_payload={'state': 'Blocked'})
        handed = self.rollback.handback()
        self.assertEqual(handed['pending_effects'], [f'cancelled:{run.attempt_id}'])
        self.assertEqual([effect['effect_id'] for effect in self.store.pending_effects()], handed['pending_effects'])
        self.assertTrue(self.store.dispatch_disabled())

    def test_rollback_preserves_records_and_local_edits(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        (workspace / 'operator-edit.txt').write_text('unrelated local edit\n')
        run = self.running()
        self.store.register_approval('approval-1', task_id=run.task_id, scope_digest=ARTIFACT.digest,
                                     max_uses=1, expires_at=self.now[0] + 100)
        self.advance(run, S.CANCELLING)
        database = (self.root / 'attempts.db')
        def rows():
            return {table: self.store.db.execute(f'SELECT * FROM {table}').fetchall().__len__()
                    for table in ('executions', 'attempts', 'approvals', 'outbox')}
        before_rows = rows()
        before_edit = hashlib.sha256((workspace / 'operator-edit.txt').read_bytes()).hexdigest()
        self.rollback.begin(actor='operator', reason='rollback')
        with self.assertRaises(ContractError):
            self.rollback.handback()
        self.assertEqual(rows(), before_rows)
        self.assertEqual(hashlib.sha256((workspace / 'operator-edit.txt').read_bytes()).hexdigest(), before_edit)
        self.assertEqual(sorted(path.name for path in workspace.iterdir()), ['operator-edit.txt'])
        self.assertTrue(database.exists())


if __name__ == '__main__':
    unittest.main()
