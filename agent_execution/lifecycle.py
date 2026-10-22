"""Pure attempt transition guards. Persistent stores must repeat these checks atomically.

Lease expiry is deliberately not a termination signal. Retry and handoff successors receive
new attempt IDs, rather than resetting terminal attempts or erasing their history.
"""
from dataclasses import replace

from .contracts import Attempt, AttemptState as S, Claim, ContractError


TRANSITIONS = {
    S.QUEUED: frozenset((S.CLAIMED, S.CANCELLED)),
    S.CLAIMED: frozenset((S.RUNNING, S.RECOVERING, S.CANCELLING)),
    S.RUNNING: frozenset((S.VERIFYING, S.AWAITING_APPROVAL, S.HANDOFF_READY, S.RECOVERING, S.CANCELLING)),
    S.AWAITING_APPROVAL: frozenset((S.RUNNING, S.CANCELLING, S.RECOVERING)),
    S.RECOVERING: frozenset((S.RUNNING, S.VERIFYING, S.BLOCKED, S.CANCELLING)),
    S.VERIFYING: frozenset((S.SUCCEEDED, S.BLOCKED, S.CANCELLING)),
    S.CANCELLING: frozenset((S.CANCELLED, S.BLOCKED)),
    S.HANDOFF_READY: frozenset(),
    S.CANCELLED: frozenset(),
    S.BLOCKED: frozenset(),
    S.SUCCEEDED: frozenset(),
}


def transition(attempt: Attempt, target: S, *, owner_id: str | None = None,
               generation: int | None = None, termination_confirmed: bool = False,
               verification_passed: bool = False, approval_confirmed: bool = False,
               checkpoint_accepted: bool = False, new_claim: Claim | None = None) -> Attempt:
    """Return validated new state; never mutate or imply durable ownership/approval itself."""
    try:
        target = S(target)
    except (ValueError, TypeError) as exc:
        raise ContractError("unknown target state") from exc
    if type(generation) not in (int, type(None)):
        raise ContractError("generation must be an integer")
    for flag in (termination_confirmed, verification_passed, approval_confirmed, checkpoint_accepted):
        if type(flag) is not bool:
            raise ContractError("transition evidence flags must be booleans")
    if attempt.claim is not None:
        if owner_id != attempt.claim.owner_id or generation != attempt.claim.generation:
            raise ContractError("stale or missing ownership identity")
    if target not in TRANSITIONS[attempt.state]:
        raise ContractError(f"transition refused: {attempt.state.value} -> {target.value}")
    if target == S.CLAIMED:
        if attempt.claim is not None or not isinstance(new_claim, Claim):
            raise ContractError("claiming requires a new claim on an unclaimed attempt")
        if owner_id != new_claim.owner_id or generation != new_claim.generation:
            raise ContractError("new claim identity mismatch")
    elif new_claim is not None:
        raise ContractError("ownership cannot change through a state transition")
    if target in (S.VERIFYING, S.HANDOFF_READY, S.SUCCEEDED) or (
        target == S.CANCELLED and attempt.state != S.QUEUED
    ):
        if not termination_confirmed:
            raise ContractError("worker termination must be confirmed")
    if target == S.HANDOFF_READY and not checkpoint_accepted:
        raise ContractError("handoff requires accepted checkpoint")
    if attempt.state == S.AWAITING_APPROVAL and target == S.RUNNING and not approval_confirmed:
        raise ContractError("resumption requires matching approval")
    if target == S.SUCCEEDED and not verification_passed:
        raise ContractError("success requires independent verification")
    return replace(attempt, state=target, claim=new_claim if target == S.CLAIMED else attempt.claim)
