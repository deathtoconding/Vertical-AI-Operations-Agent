"""Domain vocabulary — the closed vocabulary the whole system shares.

This module is the *contract* from OPS-001 (`docs/planning/domain.md`) expressed as code.
It imports nothing: no FastAPI, no SQLAlchemy, no SDKs. That is what makes the domain
testable, and it is enforced by ``tests/unit/planning/test_architecture_rules.py``.

Anything that is not enumerated here does not exist as far as the agent is concerned:
an unknown incident type cannot be created, an unknown state cannot be entered, and an
unknown risk level cannot be approved. Closed vocabularies are how a bounded agent stays
bounded.
"""

from __future__ import annotations

from enum import StrEnum

# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #


class Severity(StrEnum):
    """Customer-impact classification (docs/planning/domain.md §4)."""

    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"

    @property
    def rank(self) -> int:
        """1 is most severe; used for sorting and for 'raise severity' guards."""
        return {"SEV1": 1, "SEV2": 2, "SEV3": 3, "SEV4": 4}[self.value]

    @classmethod
    def parse(cls, value: str) -> Severity:
        try:
            return cls(value.upper())
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"unknown severity: {value!r}") from exc

    def is_pageable(self) -> bool:
        return self in (Severity.SEV1, Severity.SEV2)


class IncidentType(StrEnum):
    """Closed incident taxonomy (docs/planning/domain.md §3)."""

    API_ERROR_SPIKE = "API_ERROR_SPIKE"
    API_LATENCY_SPIKE = "API_LATENCY_SPIKE"
    TRAFFIC_ANOMALY = "TRAFFIC_ANOMALY"
    SUBSCRIPTION_PAYMENT_ANOMALY = "SUBSCRIPTION_PAYMENT_ANOMALY"
    DEPLOYMENT_REGRESSION = "DEPLOYMENT_REGRESSION"
    TOOL_FAILURE = "TOOL_FAILURE"


class IncidentStatus(StrEnum):
    """Lifecycle of an incident record (distinct from an agent run's state).

    An incident is created ``OPEN``, may be ``ACKNOWLEDGED`` by a human, and must end in
    ``RESOLVED`` or ``ESCALATED`` — invariant 6 in the domain document. There is no path
    to "silently disappeared".
    """

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"

    @property
    def is_terminal(self) -> bool:
        return self in (IncidentStatus.RESOLVED, IncidentStatus.ESCALATED)


class MetricFamily(StrEnum):
    """Metric families the detector understands (OPS-030)."""

    ERROR_RATE = "error_rate"
    LATENCY_P95 = "latency_p95"
    LATENCY_P99 = "latency_p99"
    REQUEST_RATE = "request_rate"
    PAYMENT_FAILURE_RATE = "payment_failure_rate"
    SATURATION = "saturation"


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #


class EvidenceSource(StrEnum):
    """Where an evidence item came from. Every item is source-labelled (OPS-040)."""

    METRICS = "metrics"
    LOGS = "logs"
    GITHUB = "github"
    JIRA = "jira"
    SLACK = "slack"
    DEPLOYMENT = "deployment"
    PAYMENTS = "payments"
    APPLICATION_EVENTS = "application_events"
    DETECTION = "detection"
    VERIFICATION = "verification"


class EvidenceKind(StrEnum):
    """Normalised evidence shapes — the reasoner sees these, not raw payloads."""

    METRIC_SERIES = "metric_series"
    LOG_CLUSTER = "log_cluster"
    COMMIT = "commit"
    PULL_REQUEST = "pull_request"
    DEPLOYMENT = "deployment"
    ISSUE = "issue"
    PAYMENT_FAILURE_SUMMARY = "payment_failure_summary"
    AFFECTED_CUSTOMERS = "affected_customers"
    ANOMALY = "anomaly"
    OBSERVATION = "observation"
    DEGRADATION = "degradation"


class Confidence(StrEnum):
    """Reliability of an evidence item, used for ranking and grounding checks."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def confidence_score(level: Confidence) -> float:
    """Numeric weight used when ranking evidence for the reasoner."""
    return {Confidence.HIGH: 0.9, Confidence.MEDIUM: 0.6, Confidence.LOW: 0.3}[level]


# --------------------------------------------------------------------------- #
# Agent runs
# --------------------------------------------------------------------------- #


class AgentState(StrEnum):
    """Agent run lifecycle (OPS-002 / docs/architecture/agent-runtime.md §2)."""

    NEW = "NEW"
    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    PLANNED = "PLANNED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"

    @property
    def is_terminal(self) -> bool:
        """Terminal states do not advance; ``FAILED``/``ESCALATED`` may be resumed by a human."""
        return self in (AgentState.RESOLVED, AgentState.ESCALATED)

    @property
    def is_human_gate(self) -> bool:
        return self is AgentState.WAITING_APPROVAL


# --------------------------------------------------------------------------- #
# Tools, policy and approval
# --------------------------------------------------------------------------- #


class RiskLevel(StrEnum):
    """Risk classification drives autonomy and approval (ADR-0003)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}[self.value]

    def at_least(self, other: RiskLevel) -> bool:
        return self.rank >= other.rank


class AutonomyLevel(StrEnum):
    """How much the system is allowed to do without a human (V6 roadmap gate)."""

    OBSERVE_ONLY = "observe_only"  # detect + investigate + propose, never execute
    APPROVAL_REQUIRED = "approval_required"  # default: medium risk and above need approval
    SELECTIVE_AUTONOMY = "selective_autonomy"  # low risk auto-executes; high risk still gated


class PolicyDecisionType(StrEnum):
    """Terminal outcomes of a policy evaluation (app/policy/engine.py)."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class ApprovalDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"

    @property
    def is_final(self) -> bool:
        return self is not ApprovalDecision.PENDING


class Role(StrEnum):
    """RBAC roles (SEC-002). Least privilege: viewer cannot change anything."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    SRE = "sre"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return {"viewer": 0, "operator": 1, "sre": 2, "admin": 3}[self.value]


# --------------------------------------------------------------------------- #
# Actions and verification
# --------------------------------------------------------------------------- #


class ActionStatus(StrEnum):
    """Action execution lifecycle. ``SUCCEEDED`` means "executed", NOT "verified effective"."""

    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

    @property
    def is_final(self) -> bool:
        return self in (
            ActionStatus.REJECTED,
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.SKIPPED,
        )


class VerificationOutcome(StrEnum):
    """The three honest answers (ADR-0004). Never inferred from an HTTP status."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

    @property
    def resolves_incident(self) -> bool:
        return self is VerificationOutcome.SUCCESS


class EscalationReason(StrEnum):
    """Why the agent handed the incident to a human."""

    POLICY_DENIED = "policy_denied"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_TIMEOUT = "approval_timeout"
    ACTION_FAILED = "action_failed"
    ACTION_TIMEOUT = "action_timeout"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_UNKNOWN = "verification_unknown"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    AGENT_ERROR = "agent_error"
    UNSAFE_ACTION_ATTEMPTED = "unsafe_action_attempted"
    ROLLBACK_LIMIT_REACHED = "rollback_limit_reached"


class ToolOutcome(StrEnum):
    """Outcome recorded for every tool invocation (and every rejected attempt)."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    DENIED = "denied"
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_INPUT = "invalid_input"
    SIMULATED = "simulated"


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


class AuditEventType(StrEnum):
    """Auditable event types (SEC-004). Every consequential step uses one of these."""

    INCIDENT_CREATED = "incident_created"
    INCIDENT_RESOLVED = "incident_resolved"
    INCIDENT_ESCALATED = "incident_escalated"
    ANOMALY_DETECTED = "anomaly_detected"
    ANOMALY_DEDUPLICATED = "anomaly_deduplicated"
    EVIDENCE_COLLECTED = "evidence_collected"
    EVIDENCE_SOURCE_DEGRADED = "evidence_source_degraded"
    INVESTIGATION_COMPLETED = "investigation_completed"
    INVESTIGATION_FAILED = "investigation_failed"
    DIAGNOSIS_REJECTED = "diagnosis_rejected"
    ACTION_PROPOSED = "action_proposed"
    ACTION_PROPOSAL_REJECTED = "action_proposal_rejected"
    POLICY_EVALUATED = "policy_evaluated"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_INVALIDATED = "approval_invalidated"
    ACTION_EXECUTED = "action_executed"
    ACTION_FAILED = "action_failed"
    ACTION_REPLAYED = "action_replayed"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_COMPLETED = "verification_completed"
    RUN_STATE_CHANGED = "run_state_changed"
    RUN_RECOVERED = "run_recovered"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_DENIED = "authorization_denied"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    VALIDATION_REJECTED = "validation_rejected"
    PROMPT_INJECTION_DETECTED = "prompt_injection_detected"
    UNKNOWN_TOOL_REQUESTED = "unknown_tool_requested"
    UNSAFE_ACTION_ATTEMPTED = "unsafe_action_attempted"
    AUTONOMY_LEVEL_CHANGED = "autonomy_level_changed"
    AUDIT_CHAIN_BROKEN = "audit_chain_broken"


# --------------------------------------------------------------------------- #
# Public API surface (used by docs/contract tests)
# --------------------------------------------------------------------------- #

SEVERITY_RESPONSE_TARGETS: dict[Severity, tuple[str, str]] = {
    Severity.SEV1: ("5 min", "30 min"),
    Severity.SEV2: ("15 min", "2 h"),
    Severity.SEV3: ("next business day", "3 days"),
    Severity.SEV4: ("tracked", "n/a"),
}

INCIDENT_TYPE_TO_SCENARIO: dict[IncidentType, str] = {
    IncidentType.API_ERROR_SPIKE: "A",
    IncidentType.API_LATENCY_SPIKE: "B",
    IncidentType.SUBSCRIPTION_PAYMENT_ANOMALY: "C",
}

__all__ = [
    "INCIDENT_TYPE_TO_SCENARIO",
    "SEVERITY_RESPONSE_TARGETS",
    "ActionStatus",
    "AgentState",
    "ApprovalDecision",
    "AuditEventType",
    "AutonomyLevel",
    "Confidence",
    "EscalationReason",
    "EvidenceKind",
    "EvidenceSource",
    "IncidentStatus",
    "IncidentType",
    "MetricFamily",
    "PolicyDecisionType",
    "RiskLevel",
    "Role",
    "Severity",
    "ToolOutcome",
    "VerificationOutcome",
    "confidence_score",
]
