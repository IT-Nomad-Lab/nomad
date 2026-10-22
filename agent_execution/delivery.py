"""Idempotent tracker delivery for the attempt outbox with remote effect dedupe.

The store's outbox is local and at-least-once. This module makes delivery effectively once by
asking the tracker for the stable effect ID before applying, so a crash between the remote apply
and the local acknowledgement reconciles instead of duplicating the tracker comment. Delivery
never starts, reruns or mutates an agent attempt; it only reads pending effects and acknowledges.
The transport is injected: no network or tracker client lives here.
"""
from dataclasses import dataclass
import hashlib
import json
import time
from typing import Optional, Protocol



class TransportUnavailable(Exception):
    """Tracker unreachable; nothing is known about the remote state."""


class AmbiguousResponse(Exception):
    """Tracker answer cannot prove whether the effect was applied (timeout, partial reply)."""


@dataclass(frozen=True)
class AppliedRecord:
    effect_id: str
    payload_digest: str


class TrackerTransport(Protocol):
    def find(self, effect_id: str) -> Optional[AppliedRecord]:
        """Return the applied record for this stable effect ID, or None when definitely absent.

        Raise TransportUnavailable or AmbiguousResponse when that cannot be established."""

    def apply(self, effect_id: str, task_id: str, payload: dict) -> None:
        """Apply the effect once, recording effect_id and the payload digest remotely."""


def payload_digest(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


DELIVERED = "delivered"
RECONCILED = "reconciled"      # already applied remotely with the same digest; acked without re-applying
PENDING = "pending"            # outage or ambiguous answer; retried on the next pass
CONFLICT = "conflict"          # effect ID exists remotely with a different payload; never overwritten


@dataclass(frozen=True)
class DeliveryOutcome:
    effect_id: str
    status: str
    detail: str = ""


def exponential_backoff(base=30, cap=3600):
    """Seconds to wait after the n-th failed delivery (n starts at 1)."""
    if base <= 0 or cap < base:
        raise ValueError("backoff needs 0 < base <= cap")
    return lambda failures: min(base * 2 ** (failures - 1), cap)


class TrackerDelivery:
    def __init__(self, store, transport: TrackerTransport, *, backoff=None, clock=time.time):
        if backoff is not None and not callable(backoff):
            raise TypeError("backoff must map a failure count to seconds")
        self.store, self.transport, self.backoff, self.clock = store, transport, backoff, clock

    def deliver_pending(self):
        """One pass over due effects. Conflicts wait for an operator; failures back off if configured."""
        now = self.clock()
        return [self._deliver(effect) for effect in self.store.pending_effects()
                if not effect.get("conflict") and effect.get("next_attempt_at", 0) <= now]

    def _retry_at(self, effect):
        return 0 if self.backoff is None else self.clock() + self.backoff(effect["attempts"] + 1)

    def _deliver(self, effect) -> DeliveryOutcome:
        effect_id = effect["effect_id"]
        payload = json.loads(effect["payload"])
        digest = payload_digest(payload)
        try:
            remote = self.transport.find(effect_id)
            if remote is not None:
                if remote.effect_id != effect_id:
                    raise AmbiguousResponse("remote record names a different effect")
                if remote.payload_digest != digest:
                    self.store.note_delivery(effect_id, error="payload_conflict", conflict=True)
                    return DeliveryOutcome(effect_id, CONFLICT, "remote effect has a different payload")
                self.store.note_delivery(effect_id)
                return DeliveryOutcome(effect_id, RECONCILED)
            self.transport.apply(effect_id, effect["task_id"], payload)
        except (TransportUnavailable, AmbiguousResponse) as exc:
            self.store.note_delivery(effect_id, error=type(exc).__name__, retry_at=self._retry_at(effect))
            return DeliveryOutcome(effect_id, PENDING, type(exc).__name__)
        self.store.note_delivery(effect_id)
        return DeliveryOutcome(effect_id, DELIVERED)
