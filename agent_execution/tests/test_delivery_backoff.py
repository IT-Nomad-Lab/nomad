"""Outbox conflict parking and delivery backoff, store and controller. Fake tracker and clock only."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from agent_execution.contracts import AttemptState as S, CheckResult, ContractError, ExecutionResult, ResultStatus, Usage
from agent_execution.controller import ExecutionController
from agent_execution.delivery import (
    CONFLICT, DELIVERED, PENDING, RECONCILED, TrackerDelivery, exponential_backoff, payload_digest,
)
from agent_execution.store import SCHEMA, AttemptStore
from agent_execution.tests.fixtures import ARTIFACT, SHA, task
from agent_execution.tests.test_tracker_delivery import FakeTracker


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class Outbox(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = Clock()
        self.store = AttemptStore(self.root / 'attempts.db', clock=self.clock)
        self.addCleanup(self.store.close)
        self.tracker = FakeTracker()
        self.task = replace(task(), execution_policy=replace(task().execution_policy, max_attempts=2))
        run = self.store.claim(self.task, ARTIFACT, agent_id='local', workspace=str(self.root / 'workspace'),
                               owner_id='scheduler')
        run = self.store.record_process(run.attempt_id, 'scheduler', run.claim.generation,
                                        pid=12345, start_time='fixture-process-start')
        self.store.advance(run.attempt_id, 'scheduler', run.claim.generation, S.VERIFYING,
                           termination_confirmed=True)
        result = ExecutionResult(execution_id=run.execution_id, attempt_id=run.attempt_id, task_id=run.task_id,
                                 agent_id=run.agent_id, generation=run.claim.generation,
                                 status=ResultStatus.CANDIDATE_COMPLETE, workspace=run.workspace,
                                 base_commit=run.base_commit, result_commit=SHA, usage=Usage(),
                                 test_results=(CheckResult(gate_id='G1', status='passed', evidence=ARTIFACT),))
        self.store.complete(run.attempt_id, 'scheduler', run.claim.generation, result,
                            verification_ref=ARTIFACT, tracker_payload={'state': 'Done'})
        self.effect_id = f'complete:{run.attempt_id}'

    def delivery(self, backoff=exponential_backoff(30, 3600)):
        return TrackerDelivery(self.store, self.tracker, backoff=backoff, clock=self.clock)

    def statuses(self, delivery):
        return [o.status for o in delivery.deliver_pending()]



class OutboxDelivery(Outbox):
    def test_an_outage_backs_off_exponentially_without_calling_the_tracker_early(self):
        delivery = self.delivery()
        self.tracker.down = True
        finds = Mock(side_effect=self.tracker.find)
        self.tracker.find = finds
        waits = []
        for _ in range(4):
            self.assertEqual(self.statuses(delivery), [PENDING])
            (effect,) = self.store.pending_effects()
            waits.append(effect['next_attempt_at'] - self.clock.now)
            calls = finds.call_count
            self.clock.now += waits[-1] - 1
            self.assertEqual(self.statuses(delivery), [])
            self.assertEqual(finds.call_count, calls)
            self.clock.now += 1
        self.assertEqual(waits, [30, 60, 120, 240])
        self.assertEqual(self.store.pending_effects()[0]['attempts'], 4)
        self.tracker.down = False
        self.assertEqual(self.statuses(delivery), [DELIVERED])
        self.assertEqual(self.store.pending_effects(), [])
        self.assertEqual(self.tracker.applies, 1)

    def test_backoff_is_capped(self):
        backoff = exponential_backoff(30, 3600)
        self.assertEqual([backoff(n) for n in (1, 2, 7, 8, 40)], [30, 60, 1920, 3600, 3600])
        for base, cap in ((0, 10), (-1, 10), (60, 30)):
            with self.assertRaises(ValueError):
                exponential_backoff(base, cap)
        with self.assertRaises(TypeError):
            TrackerDelivery(self.store, self.tracker, backoff=30)

    def test_a_conflict_is_parked_for_an_operator_and_never_retried(self):
        self.tracker.comments[self.effect_id] = (payload_digest({'state': 'Other'}), {'state': 'Other'})
        delivery = self.delivery()
        self.assertEqual(self.statuses(delivery), [CONFLICT])
        self.tracker.find = Mock(side_effect=AssertionError('a parked conflict must not be retried'))
        self.clock.now += 10 ** 6
        self.assertEqual(self.statuses(delivery), [])
        self.assertEqual(self.tracker.applies, 0)
        (parked,) = self.store.conflicted_effects()
        self.assertEqual((parked['effect_id'], parked['last_error'], parked['conflict']),
                         (self.effect_id, 'payload_conflict', 1))
        self.assertEqual([e['effect_id'] for e in self.store.pending_effects()], [self.effect_id])
        self.assertEqual(self.store.rollback_inventory()['pending_effects'], [self.effect_id])

    def test_note_delivery_refuses_inconsistent_outcomes(self):
        for kwargs in ({'conflict': True}, {'retry_at': self.clock.now + 5}, {'error': 'x', 'retry_at': -1},
                       {'error': 'x', 'retry_at': '5'}, {'error': 'x', 'retry_at': True}):
            with self.assertRaises(ContractError):
                self.store.note_delivery(self.effect_id, **kwargs)
        (effect,) = self.store.pending_effects()
        self.assertEqual((effect['attempts'], effect['conflict'], effect['next_attempt_at']), (0, 0, 0))

    def test_notes_after_delivery_are_ignored(self):
        self.assertEqual(self.statuses(self.delivery()), [DELIVERED])
        self.store.note_delivery(self.effect_id, error='late', conflict=True, retry_at=self.clock.now + 5)
        self.assertEqual(self.store.pending_effects(), [])
        self.assertEqual(self.store.conflicted_effects(), [])

    def test_without_backoff_a_failed_effect_is_due_again_immediately(self):
        delivery = self.delivery(backoff=None)
        self.tracker.down = True
        self.assertEqual(self.statuses(delivery), [PENDING])
        self.assertEqual(self.statuses(delivery), [PENDING])
        self.assertEqual(self.store.pending_effects()[0]['next_attempt_at'], 0)
        self.assertEqual(self.store.conflicted_effects(), [])


class OutboxColumnMigration(unittest.TestCase):
    def test_an_existing_version_three_outbox_gains_the_columns_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'attempts.db'
            old = sqlite3.connect(path)
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    old.execute(statement)
            old.execute("INSERT INTO outbox(effect_id,task_id,payload,attempts,last_error) "
                        "VALUES('e1','t1','{}',2,'TransportUnavailable')")
            old.execute('PRAGMA user_version=3')
            old.commit()
            old.close()
            store = AttemptStore(path)
            try:
                (row,) = store.pending_effects()
                self.assertEqual((row['effect_id'], row['attempts'], row['conflict'], row['next_attempt_at']),
                                 ('e1', 2, 0, 0))
                self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 3)
            finally:
                store.close()
            AttemptStore(path).close()


class ControllerTrackerDelivery(Outbox):
    def controller(self):
        controller = ExecutionController(self.store, None, {}, authorize=Mock())
        self.addCleanup(controller.close)
        return controller

    def test_controller_delivers_with_remote_dedupe_and_default_backoff(self):
        controller = self.controller()
        self.tracker.ambiguous_apply = True
        self.assertEqual([o.status for o in controller.deliver_tracker(self.tracker)], [PENDING])
        self.assertEqual(self.tracker.applies, 1)
        self.assertEqual(controller.deliver_tracker(self.tracker), [])
        self.clock.now += 30
        self.tracker.ambiguous_apply = False
        self.assertEqual([o.status for o in controller.deliver_tracker(self.tracker)], [RECONCILED])
        self.assertEqual(self.tracker.applies, 1)
        self.assertEqual(self.store.pending_effects(), [])

    def test_a_closed_controller_refuses_delivery(self):
        controller = self.controller()
        controller.close()
        with self.assertRaises(ContractError):
            controller.deliver_tracker(self.tracker)


if __name__ == '__main__':
    unittest.main()
