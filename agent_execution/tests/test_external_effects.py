"""Uncertain external effects are durable, block further effects and completion, and need an operator."""
import sqlite3

from agent_execution.contracts import AttemptState as S, ContractError
from agent_execution.rollback import RollbackCoordinator
from agent_execution.tests.fixtures import ARTIFACT
from agent_execution.tests.test_store import DurableAttempts
from agent_execution.tests.test_tool_authority import AuthorityFixture


class EffectsThroughAuthority(AuthorityFixture):
    arguments = {'task': 'task:123', 'body': 'approved content'}

    def second_approval(self):
        from agent_execution.policy import OperatorApproval, action_scope
        scope = action_scope(self.task, tool=self.route.tool, action=self.route.action,
                             target=self.arguments['task'], arguments=self.arguments)
        self.policy.register_approval(OperatorApproval(approval_id='second', actor_id='operator', scope=scope,
                                                       expires_at='1970-01-01T00:30:00Z'), authenticated_actor='operator')
        return 'second'

    def uses(self, approval):
        return self.store.db.execute('SELECT uses FROM approvals WHERE approval_id=?', (approval,)).fetchone()[0]

    def test_successful_effect_is_recorded_done(self):
        self.authority.handle(self.request(self.route.tool, self.arguments, approval_id=self.approve(self.arguments)))
        rows = self.store.db.execute('SELECT tool, state, approval_id FROM external_effects').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('mcp:tracker:comment', 'done', 'approved-comment')])
        self.assertEqual(self.store.unreconciled_effects(self.run.attempt_id), [])

    def test_failed_handler_blocks_later_effects_without_spending_their_approval(self):
        self.dispatch.side_effect = OSError('remote acknowledgement lost')
        with self.assertRaises(OSError):
            self.authority.handle(self.request(self.route.tool, self.arguments, approval_id=self.approve(self.arguments)))
        [effect] = self.store.unreconciled_effects(self.run.attempt_id)
        self.assertEqual(effect['state'], 'uncertain')
        self.dispatch.side_effect = None
        second = self.second_approval()
        with self.assertRaises(ContractError):
            self.authority.handle(self.request(self.route.tool, self.arguments, approval_id=second))
        self.assertEqual(self.uses(second), 0)
        self.assertEqual(self.dispatch.call_count, 1)
        with self.assertRaises(ContractError):
            self.policy.reconcile_effect(effect['effect_id'], outcome='not_applied', authenticated_actor='agent')
        self.policy.reconcile_effect(effect['effect_id'], outcome='not_applied', authenticated_actor='operator')
        self.authority.handle(self.request(self.route.tool, self.arguments, approval_id=second))
        self.assertEqual(self.dispatch.call_count, 2)


class EffectsInStore(DurableAttempts):
    def begin(self, run, store=None):
        return (store or self.store).begin_external_effect(run.attempt_id, 'scheduler', run.claim.generation,
                                                           tool='mcp:tracker:comment', scope_digest=ARTIFACT.digest)

    def complete(self, run, store=None):
        return (store or self.store).complete(run.attempt_id, 'scheduler', run.claim.generation, self.result(run),
                                              verification_ref=ARTIFACT, tracker_payload={'state': 'Done'})

    def test_crash_mid_dispatch_blocks_completion_and_handback_until_reconciled(self):
        run = self.running()
        effect = self.begin(run)
        self.advance(run, S.VERIFYING, termination_confirmed=True)
        reopened = self.open()
        with self.assertRaisesRegex(ContractError, 'unreconciled external effect'):
            self.complete(run, reopened)
        rollback = RollbackCoordinator(reopened)
        rollback.begin(actor='operator', reason='fixture')
        found, _ = rollback.blockers()
        self.assertTrue(any(effect in item and 'dispatching' in item for item in found))
        for bad in (dict(actor='operator', outcome='maybe'), dict(actor='', outcome='applied')):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                reopened.reconcile_external_effect(effect, **bad)
        reopened.reconcile_external_effect(effect, actor='operator', outcome='applied')
        with self.assertRaises(ContractError):
            reopened.reconcile_external_effect(effect, actor='operator', outcome='not_applied')
        self.assertEqual(self.complete(run, reopened).state, S.SUCCEEDED)
        self.assertEqual(reopened.rollback_inventory()['uncertain_effects'], [])

    def test_finish_is_single_and_done_needs_no_reconciliation(self):
        run = self.running()
        effect = self.begin(run)
        self.store.finish_external_effect(effect, certain=True)
        for certain in (True, False):
            with self.assertRaises(ContractError):
                self.store.finish_external_effect(effect, certain=certain)
        with self.assertRaises(ContractError):
            self.store.reconcile_external_effect(effect, actor='operator', outcome='applied')
        self.advance(run, S.VERIFYING, termination_confirmed=True)
        self.assertEqual(self.complete(run).state, S.SUCCEEDED)

    def test_store_refuses_a_new_effect_while_one_is_uncertain(self):
        run = self.running()
        effect = self.begin(run)
        self.store.finish_external_effect(effect, certain=False)
        with self.assertRaisesRegex(ContractError, 'uncertain'):
            self.begin(run)
        self.store.reconcile_external_effect(effect, actor='operator', outcome='applied')
        self.store.finish_external_effect(self.begin(run), certain=True)

    def test_stale_generation_cannot_begin_an_effect(self):
        run = self.running()
        with self.assertRaises(ContractError):
            self.store.begin_external_effect(run.attempt_id, 'scheduler', run.claim.generation + 1,
                                             tool='mcp:tracker:comment', scope_digest=ARTIFACT.digest)
        with self.assertRaises(ContractError):
            self.store.begin_external_effect(run.attempt_id, 'intruder', run.claim.generation,
                                             tool='mcp:tracker:comment', scope_digest=ARTIFACT.digest)

    def test_version_two_database_gains_effect_tracking_and_older_writers_are_fenced(self):
        run = self.running()
        self.store.close()
        previous = sqlite3.connect(self.root / 'attempts.db')
        previous.execute('DROP TABLE external_effects')
        previous.execute('PRAGMA user_version=2')
        previous.commit()
        previous.close()
        migrated = self.open()
        self.assertEqual(migrated.db.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertEqual(migrated.get(run.attempt_id), run)
        self.begin(run, migrated)
