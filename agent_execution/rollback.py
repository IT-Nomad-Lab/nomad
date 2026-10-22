"""Staged rollback coordinator for the hybrid execution path (#1463 RB01-RB03).

Rollback stops new dispatch first, then reports what still holds a workspace. It never
deletes attempts, artifacts, worktrees, approvals or pending outbox effects, and never
invents compensating effects. Writers are reconciled through the controller's existing
cancel/recover/reconcile operations; uncertain ones stay quarantined and block handback.

Schema rollback (RB04, V22) lowers the store's version only after the same blockers clear,
re-checked inside one write transaction, so an older writer never opens live newer state; no
table, column or row is dropped. GPU restoration (RB05) is never performed here: a pending
media or gpu_guard restore-state file blocks handback and schema rollback until its own
recovery removes it. The controller's lease loads and evicts nothing, so it owns nothing to restore.
"""
import os

from .contracts import ContractError


def store_blockers(inventory):
    """Human-readable reasons, from a store inventory, that a hybrid writer may still act."""
    found = []
    if not inventory['dispatch_disabled']:
        found.append('new dispatch is still enabled')
    found += [f"attempt {item['attempt_id']} ({item['state']}) not proven terminated"
              for item in inventory['unterminated']]
    found += [f"workspace {item['workspace']} still reserved by {item['attempt_id']}"
              for item in inventory['reservations']]
    found += [f'verifier for {attempt_id} not reconciled' for attempt_id in inventory['verifiers']]
    found += [f"external effect {item['effect_id']} ({item['tool']}) of {item['attempt_id']} is {item['state']}"
              for item in inventory['uncertain_effects']]
    return found


class RollbackCoordinator:
    def __init__(self, store, *, gpu_restore_paths=()):
        self.store = store
        self.gpu_restore_paths = tuple(os.fspath(path) for path in gpu_restore_paths)

    def begin(self, *, actor, reason):
        """Idempotent and durable: survives restart because the gate lives in the attempt store."""
        self.store.set_dispatch(enabled=False, actor=actor, reason=reason)
        return self.store.rollback_inventory()

    def blockers(self):
        inventory = self.store.rollback_inventory()
        return store_blockers(inventory) + self.gpu_blockers(), inventory

    def gpu_blockers(self):
        found = []
        for path in self.gpu_restore_paths:
            try:
                os.lstat(path)
            except FileNotFoundError:
                continue
            found.append(f'GPU restoration pending ({path}); run its own recovery first')
        return found

    def handback(self):
        """Legacy ownership only when no hybrid writer can still touch a workspace.

        Pending effects do not block handback; they are retained and returned so delivery
        continues independently of execution ownership.
        """
        found, inventory = self.blockers()
        if found:
            raise ContractError('legacy handback refused: ' + '; '.join(found))
        return {'pending_effects': inventory['pending_effects']}

    def resume(self, *, actor, reason):
        self.store.set_dispatch(enabled=True, actor=actor, reason=reason)

    def schema_rollback(self, target_version, *, actor, reason):
        """Let an older writer open the store once nothing newer can still act. Records stay."""
        found = self.gpu_blockers()
        if found:
            raise ContractError('schema rollback refused: ' + '; '.join(found))
        return self.store.rollback_schema(target_version, actor=actor, reason=reason)
