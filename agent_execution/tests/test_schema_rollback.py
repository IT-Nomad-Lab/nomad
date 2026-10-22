"""Schema rollback fencing (RB04, V22) and pending GPU restoration as a rollback blocker (RB05)."""
import sqlite3
import unittest
from unittest.mock import patch

from agent_execution.contracts import AttemptState as S, ContractError
from agent_execution.rollback import RollbackCoordinator
from agent_execution.store import SCHEMA_VERSION, AttemptStore
from agent_execution.tests.fixtures import ARTIFACT
from agent_execution.tests import test_rollback

TABLES = ('executions', 'attempts', 'approvals', 'outbox', 'external_effects', 'verification', 'reservations')


class Fixture(unittest.TestCase):
    # Borrow the rollback fixture helpers without re-collecting StagedRollback's own tests.
    _helpers = test_rollback.StagedRollback
    open, claim, advance, running, result = (_helpers.open, _helpers.claim, _helpers.advance,
                                             _helpers.running, _helpers.result)

    def setUp(self):
        self._helpers.setUp(self)
        self.gpu_state = self.root / 'gpu_guard_state.json'
        self.rollback = RollbackCoordinator(self.store, gpu_restore_paths=(self.gpu_state,))

    def version(self, path=None):
        db = sqlite3.connect(path or self.root / 'attempts.db')
        try:
            return db.execute('PRAGMA user_version').fetchone()[0]
        finally:
            db.close()

    def counts(self):
        return {table: self.store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in TABLES}

    def cancelled(self):
        run = self.running()
        self.advance(run, S.CANCELLING)
        return run

    def finish(self, run):
        self.store.finish_cancellation(run.attempt_id, 'scheduler', run.claim.generation,
                                       termination_confirmed=True, tracker_payload={'state': 'Blocked'})


class SchemaRollback(Fixture):
    def refused(self, match):
        before = self.counts()
        with self.assertRaisesRegex(ContractError, match):
            self.rollback.schema_rollback(2, actor='operator', reason='revert to v2 writer')
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM schema_rollbacks').fetchone()[0], 0)

    def test_refused_while_dispatch_is_enabled(self):
        self.refused('dispatch is still enabled')

    def test_refused_while_a_newer_writer_is_unterminated_or_reserved(self):
        self.cancelled()
        self.rollback.begin(actor='operator', reason='rollback')
        self.refused('not proven terminated.*still reserved')

    def test_refused_while_a_verifier_claim_is_live(self):
        run = self.running()
        self.store.accept_result(run.attempt_id, 'scheduler', run.claim.generation, self.result(run))
        self.advance(run, S.VERIFYING, termination_confirmed=True)
        self.store.begin_verification(run.attempt_id, 'scheduler', run.claim.generation)
        self.rollback.begin(actor='operator', reason='rollback')
        self.refused('verifier for .* not reconciled')

    def test_refused_while_an_external_effect_is_uncertain(self):
        run = self.running()
        effect = self.store.begin_external_effect(run.attempt_id, 'scheduler', run.claim.generation,
                                                  tool='tracker.comment', scope_digest=ARTIFACT.digest)
        self.store.finish_external_effect(effect, certain=False)
        self.rollback.begin(actor='operator', reason='rollback')
        self.refused(f'external effect {effect} \\(tracker.comment\\)')
        self.store.reconcile_external_effect(effect, actor='operator', outcome='not_applied')
        self.advance(run, S.CANCELLING)
        self.finish(run)
        self.assertEqual(self.rollback.schema_rollback(2, actor='operator', reason='after reconcile'), 2)

    def test_the_store_rechecks_without_the_coordinator(self):
        with self.assertRaisesRegex(ContractError, 'dispatch is still enabled'):
            self.store.rollback_schema(2, actor='operator', reason='direct')
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_blockers_are_read_under_the_write_lock_so_no_writer_slips_in(self):
        self.rollback.begin(actor='operator', reason='rollback')
        inventory, competing = self.store.rollback_inventory, []

        def read_while_a_second_writer_tries():
            other = sqlite3.connect(self.root / 'attempts.db', timeout=0.05, isolation_level=None)
            try:
                other.execute('BEGIN IMMEDIATE')
                other.execute('ROLLBACK')
                competing.append('got in')
            except sqlite3.OperationalError as exc:
                competing.append(str(exc))
            finally:
                other.close()
            return inventory()

        with patch.object(self.store, 'rollback_inventory', side_effect=read_while_a_second_writer_tries):
            self.store.rollback_schema(2, actor='operator', reason='locked check')
        self.assertEqual(competing, ['database is locked'])

    def test_reconciled_store_rolls_back_retaining_every_record_and_migrates_forward_again(self):
        run = self.cancelled()
        self.store.register_approval('approval-1', task_id=run.task_id, scope_digest=ARTIFACT.digest,
                                     max_uses=1, expires_at=self.now[0] + 100)
        self.rollback.begin(actor='operator', reason='rollback')
        self.finish(run)
        before = self.counts()
        self.assertEqual(self.rollback.schema_rollback(2, actor='operator', reason='revert to v2 writer'), 2)
        self.assertEqual(self.version(), 2)
        self.assertEqual(self.counts(), before)
        self.assertEqual([e['effect_id'] for e in self.store.pending_effects()], [f'cancelled:{run.attempt_id}'])
        audit = self.store.db.execute('SELECT * FROM schema_rollbacks').fetchall()
        self.assertEqual([tuple(row) for row in audit], [(self.now[0], SCHEMA_VERSION, 2, 'operator',
                                                          'revert to v2 writer')])
        with self.assertRaisesRegex(ContractError, 'already rolled back'):
            self.store.rollback_schema(1, actor='operator', reason='again')
        reopened = AttemptStore(self.root / 'attempts.db')
        self.addCleanup(reopened.close)
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(reopened.get(run.attempt_id).state, S.CANCELLED)
        self.assertEqual(len(reopened.db.execute('SELECT * FROM schema_rollbacks').fetchall()), 1)

    def test_target_actor_and_reason_are_validated(self):
        self.rollback.begin(actor='operator', reason='rollback')
        for target in (0, SCHEMA_VERSION, SCHEMA_VERSION + 1, '2', 2.0, True):
            with self.subTest(target=target), self.assertRaisesRegex(ContractError, 'unsupported schema rollback'):
                self.store.rollback_schema(target, actor='operator', reason='x')
        for actor, reason in (('', 'x'), (' ', 'x'), ('operator', ''), (None, 'x'), ('operator', None)):
            with self.subTest(actor=actor, reason=reason), self.assertRaisesRegex(ContractError, 'actor and a reason'):
                self.store.rollback_schema(2, actor=actor, reason=reason)
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(self.store.rollback_schema(1, actor='operator', reason='oldest'), 1)


class GpuRestoreBlocker(Fixture):
    def test_a_pending_restore_blocks_handback_and_schema_rollback_and_is_never_removed(self):
        self.rollback.begin(actor='operator', reason='rollback')
        self.gpu_state.write_text('{"evicted": ["private"]}')
        with self.assertRaisesRegex(ContractError, 'GPU restoration pending'):
            self.rollback.handback()
        with self.assertRaisesRegex(ContractError, 'GPU restoration pending'):
            self.rollback.schema_rollback(2, actor='operator', reason='x')
        self.assertEqual(self.gpu_state.read_text(), '{"evicted": ["private"]}')
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.gpu_state.unlink()
        self.assertEqual(self.rollback.handback(), {'pending_effects': []})

    def test_a_dangling_symlink_still_counts_as_pending(self):
        self.rollback.begin(actor='operator', reason='rollback')
        self.gpu_state.symlink_to(self.root / 'missing')
        found, _ = self.rollback.blockers()
        self.assertEqual(len(found), 1)
        self.assertIn(str(self.gpu_state), found[0])

    def test_no_configured_paths_means_no_gpu_blocker(self):
        self.gpu_state.write_text('{}')
        self.assertEqual(RollbackCoordinator(self.store).gpu_blockers(), [])


if __name__ == '__main__':
    unittest.main()
