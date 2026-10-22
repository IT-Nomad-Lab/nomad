"""Tracker outbox delivery against a fake in-memory tracker. No network, no agent."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from agent_execution.contracts import CheckResult, ContractError, ExecutionResult, ResultStatus, Usage
from agent_execution.contracts import AttemptState as S
from agent_execution.delivery import (
    CONFLICT, DELIVERED, PENDING, RECONCILED, AmbiguousResponse, AppliedRecord,
    TrackerDelivery as Deliverer, TransportUnavailable, payload_digest,
)
from agent_execution.store import AttemptStore
from agent_execution.tests.fixtures import ARTIFACT, SHA, task


class FakeTracker:
    def __init__(self):
        self.comments = {}          # effect_id -> (digest, payload); one comment per effect
        self.applies = 0
        self.down = False
        self.ambiguous_find = False
        self.ambiguous_apply = False   # applies, then the reply is lost

    def find(self, effect_id):
        if self.down:
            raise TransportUnavailable()
        if self.ambiguous_find:
            raise AmbiguousResponse()
        got = self.comments.get(effect_id)
        return None if got is None else AppliedRecord(effect_id, got[0])

    def apply(self, effect_id, task_id, payload):
        if self.down:
            raise TransportUnavailable()
        self.applies += 1
        self.comments[effect_id] = (payload_digest(payload), payload)
        if self.ambiguous_apply:
            raise AmbiguousResponse()


class CrashOnAck:
    """Store wrapper whose acknowledgement fails once: the process dies after the remote apply."""
    def __init__(self, store):
        self.store, self.armed = store, True

    def pending_effects(self):
        return self.store.pending_effects()

    def note_delivery(self, effect_id, *, error=None):
        if self.armed and error is None:
            self.armed = False
            raise RuntimeError("simulated crash before local ack")
        return self.store.note_delivery(effect_id, error=error)


class TrackerDelivery(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.task = replace(task(), execution_policy=replace(task().execution_policy, max_attempts=2))
        self.store = self.open()
        self.tracker = FakeTracker()
        run = self.store.claim(self.task, ARTIFACT, agent_id="local",
                               workspace=str(self.root / "workspace"), owner_id="scheduler")
        run = self.store.record_process(run.attempt_id, "scheduler", run.claim.generation,
                                        pid=12345, start_time="fixture-process-start")
        self.store.advance(run.attempt_id, "scheduler", run.claim.generation, S.VERIFYING,
                           termination_confirmed=True)
        result = ExecutionResult(execution_id=run.execution_id, attempt_id=run.attempt_id,
                                 task_id=run.task_id, agent_id=run.agent_id, generation=run.claim.generation,
                                 status=ResultStatus.CANDIDATE_COMPLETE, workspace=run.workspace,
                                 base_commit=run.base_commit, result_commit=SHA, usage=Usage(),
                                 test_results=(CheckResult(gate_id="G1", status="passed", evidence=ARTIFACT),))
        self.store.complete(run.attempt_id, "scheduler", run.claim.generation, result,
                            verification_ref=ARTIFACT, tracker_payload={"state": "Done"})
        self.run_id = run.attempt_id
        self.effect_id = f"complete:{run.attempt_id}"

    def open(self):
        store = AttemptStore(self.root / "attempts.db")
        self.addCleanup(store.close)
        return store

    def attempts(self, store):
        return store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]

    def test_outage_retries_delivery_without_new_attempt(self):
        before = self.attempts(self.store)
        self.tracker.down = True
        delivery = Deliverer(self.store, self.tracker)
        for _ in range(2):
            self.assertEqual([o.status for o in delivery.deliver_pending()], [PENDING])
        self.assertEqual(self.tracker.comments, {})
        pending = self.store.pending_effects()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 2)
        self.tracker.down = False
        self.assertEqual([o.status for o in delivery.deliver_pending()], [DELIVERED])
        self.assertEqual(self.store.pending_effects(), [])
        self.assertEqual(self.tracker.applies, 1)
        self.assertEqual(self.attempts(self.store), before)
        self.assertEqual(self.store.get(self.run_id).state, S.SUCCEEDED)

    def test_remote_apply_then_local_ack_crash_reconciles_without_duplicate_comment(self):
        with self.assertRaises(RuntimeError):
            Deliverer(CrashOnAck(self.store), self.tracker).deliver_pending()
        self.assertEqual(self.tracker.applies, 1)
        self.assertEqual(len(self.store.pending_effects()), 1)
        outcomes = Deliverer(self.store, self.tracker).deliver_pending()
        self.assertEqual([o.status for o in outcomes], [RECONCILED])
        self.assertEqual(self.tracker.applies, 1)
        self.assertEqual(len(self.tracker.comments), 1)
        self.assertEqual(self.store.pending_effects(), [])

    def test_delivered_effect_survives_process_restart(self):
        Deliverer(self.store, self.tracker).deliver_pending()
        self.store.close()
        reopened = self.open()
        self.assertEqual(reopened.pending_effects(), [])
        self.assertEqual(Deliverer(reopened, self.tracker).deliver_pending(), [])
        self.assertEqual(self.tracker.applies, 1)
        with self.assertRaises(ContractError):
            reopened.enqueue_effect(self.effect_id, self.task.task_id, {"state": "Changed"})

    def test_effect_id_cannot_be_reused_with_changed_payload(self):
        # Local side: the store refuses to rebind the effect identity.
        with self.assertRaises(ContractError):
            self.store.enqueue_effect(self.effect_id, self.task.task_id, {"state": "Changed"})
        # Remote side: a record with a different digest is never overwritten and stays pending.
        self.tracker.comments[self.effect_id] = (payload_digest({"state": "Other"}), {"state": "Other"})
        outcomes = Deliverer(self.store, self.tracker).deliver_pending()
        self.assertEqual([o.status for o in outcomes], [CONFLICT])
        self.assertEqual(self.tracker.applies, 0)
        self.assertEqual(self.tracker.comments[self.effect_id][1], {"state": "Other"})
        pending = self.store.pending_effects()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["last_error"], "payload_conflict")

    def test_ambiguous_delivery_remains_pending_until_reconciled(self):
        delivery = Deliverer(self.store, self.tracker)
        self.tracker.ambiguous_find = True
        self.assertEqual([o.status for o in delivery.deliver_pending()], [PENDING])
        self.assertEqual(self.tracker.applies, 0)
        self.tracker.ambiguous_find = False
        self.tracker.ambiguous_apply = True     # applied remotely, reply lost
        self.assertEqual([o.status for o in delivery.deliver_pending()], [PENDING])
        self.assertEqual(len(self.store.pending_effects()), 1)
        self.tracker.ambiguous_apply = False
        self.assertEqual([o.status for o in delivery.deliver_pending()], [RECONCILED])
        self.assertEqual(self.tracker.applies, 1)
        self.assertEqual(self.store.pending_effects(), [])


if __name__ == "__main__":
    unittest.main()
