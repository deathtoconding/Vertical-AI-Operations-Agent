"""Action planning, execution and approval domain objects (EPIC-07)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.domain.base import DomainModel, ORMBackedModel, ensure_utc, utcnow
from app.domain.enums import ActionStatus, ApprovalDecision, RiskLevel


def canonical_hash(payload: dict[str, Any]) -> str:
    """Stable hash of a payload — the binding between an action and its approval.

    Keys are sorted and separators normalised so that semantically identical payloads hash
    identically, and any real change (a different release id, a different channel) changes
    the hash and therefore invalidates the approval (threat T-09).
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def action_payload(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """The payload an approval is bound to: the tool *and* its parameters.

    Both the approval and the executor hash this exact structure, so a change to either the
    tool or a parameter invalidates the approval (threat T-09).
    """
    return {"tool_name": tool_name, "params": params}


def derive_idempotency_key(incident_id: str, tool_name: str, params: dict[str, Any]) -> str:
    """Idempotency key: identical intent replays; different intent re-executes."""
    return hashlib.sha256(
        f"{incident_id}|{tool_name}|{canonical_hash(params)}".encode()
    ).hexdigest()[:48]


class ExpectedState(DomainModel):
    """What the world should look like if this action worked (declared *before* execution).

    Declared up front because deriving it afterwards from the observed state is how systems
    end up "verifying" that whatever happened was what they intended.
    """

    checks: list[dict[str, Any]] = Field(default_factory=list)
    metric: str | None = None
    threshold: float | None = None
    comparison: str = "below"  # below | above | equals
    target_release: str | None = None
    window_seconds: float | None = None
    window_minutes: int | None = None
    service: str | None = None
    description: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.checks


class ActionRequest(DomainModel):
    """A planner-produced action proposal (pre-policy, pre-approval)."""

    incident_id: str
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    expected_state: ExpectedState = Field(default_factory=ExpectedState)
    sequence: int = 0

    @property
    def canonical_hash(self) -> str:
        return canonical_hash(action_payload(self.tool_name, self.params))

    @property
    def idempotency_key(self) -> str:
        return derive_idempotency_key(self.incident_id, self.tool_name, self.params)


class Action(ORMBackedModel):
    """A persisted action with its policy verdict and execution state."""

    id: str
    incident_id: str
    run_id: str | None = None
    tool_name: str
    risk: RiskLevel
    permission: str
    params: dict[str, Any] = Field(default_factory=dict)
    canonical_hash: str
    idempotency_key: str
    rationale: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    status: ActionStatus = ActionStatus.PROPOSED
    attempts: int = 0
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    expected_state: dict[str, Any] = Field(default_factory=dict)
    policy_decision: str = ""
    policy_reason: str = ""
    executed_at: datetime | None = None
    simulated: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def is_high_risk(self) -> bool:
        return self.risk.at_least(RiskLevel.HIGH)

    @property
    def payload(self) -> dict[str, Any]:
        return action_payload(self.tool_name, self.params)

    @property
    def payload_is_intact(self) -> bool:
        """True when the stored hash still matches the stored payload (tamper check)."""
        return self.canonical_hash == canonical_hash(self.payload)


class ActionPlan(DomainModel):
    """The ordered set of actions for an incident, plus what was rejected."""

    incident_id: str
    run_id: str | None = None
    actions: list[Action] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    rationale: str = ""
    requires_approval: bool = False
    expected_state: ExpectedState = Field(default_factory=ExpectedState)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_empty(self) -> bool:
        return not self.actions

    def pending_approval(self) -> list[Action]:
        return [item for item in self.actions if item.requires_approval]


class Approval(ORMBackedModel):
    """A human decision bound to one payload hash."""

    id: str
    action_id: str
    incident_id: str
    run_id: str | None = None
    payload_hash: str
    decision: ApprovalDecision = ApprovalDecision.PENDING
    actor: str | None = None
    role: str | None = None
    reason: str = ""
    requested_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None

    @field_validator("requested_at", "expires_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @property
    def is_expired(self) -> bool:
        return self.decision is ApprovalDecision.PENDING and utcnow() > self.expires_at

    def matches(self, action_hash: str) -> bool:
        """The approval is only valid for the exact payload that was approved."""
        return self.payload_hash == action_hash


__all__ = [
    "Action",
    "ActionPlan",
    "ActionRequest",
    "Approval",
    "ExpectedState",
    "action_payload",
    "canonical_hash",
    "derive_idempotency_key",
]
