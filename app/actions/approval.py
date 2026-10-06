"""Approval requests and their binding (OPS-061).

An approval is only meaningful if it is *specific*:

* it names exactly one action;
* it is valid only for the payload hash recorded when it was requested — a changed parameter
  invalidates it (threat T-09: approve a harmless release, execute another);
* it expires, because an approval granted at 03:00 for an incident that has since changed is
  not consent for the current state;
* it can be decided exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.domain.actions import Action, action_payload, canonical_hash
from app.domain.enums import ApprovalDecision
from app.persistence.models.base import utcnow


@dataclass(frozen=True)
class ApprovalRequest:
    """Row values for a new approval request bound to one action payload."""

    approval_id: str
    values: dict[str, object]

    @property
    def payload_hash(self) -> str:
        return str(self.values["payload_hash"])


def build_approval_request(
    action: Action,
    *,
    approval_id: str,
    timeout_seconds: float,
) -> ApprovalRequest:
    """Create the request that a human will decide on."""
    now = utcnow()
    return ApprovalRequest(
        approval_id=approval_id,
        values={
            "id": approval_id,
            "action_id": action.id,
            "incident_id": action.incident_id,
            "run_id": action.run_id,
            "payload_hash": action.canonical_hash,
            "decision": ApprovalDecision.PENDING.value,
            "requested_at": now,
            "expires_at": now + timedelta(seconds=timeout_seconds),
        },
    )


def binds(approval: object, action: Action) -> bool:
    """True when an approval still authorises *this exact* payload.

    Checks, in order: decided APPROVED, unexpired, and hash-bound. A helper rather than an
    inline expression so the three conditions cannot be partially applied at a call site.
    """
    decision = getattr(approval, "decision", None)
    expires_at = getattr(approval, "expires_at", None)
    payload_hash = getattr(approval, "payload_hash", None)
    if decision is not ApprovalDecision.APPROVED:
        return False
    if expires_at is None or expires_at < utcnow():
        return False
    expected = canonical_hash(action_payload(action.tool_name, action.params))
    return bool(payload_hash == expected)


__all__ = ["ApprovalRequest", "binds", "build_approval_request"]
