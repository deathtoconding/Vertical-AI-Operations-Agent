"""Incident and anomaly domain objects (OPS-001, OPS-031)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.domain.base import ORMBackedModel, ensure_utc, utcnow
from app.domain.enums import IncidentStatus, IncidentType, MetricFamily, Severity


class Incident(ORMBackedModel):
    """The durable record of one operational problem."""

    id: str
    incident_type: IncidentType
    severity: Severity
    status: IncidentStatus = IncidentStatus.OPEN
    title: str
    summary: str = ""
    service: str
    metric: str | None = None
    dedup_key: str
    detected_at: datetime
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    agent_run_id: str | None = None
    diagnosis: dict[str, Any] | None = None
    verification_outcome: str | None = None
    escalation_reason: str | None = None
    jira_issue_key: str | None = None
    slack_message_ts: str | None = None
    evidence_count: int = 0
    rollback_count: int = 0
    simulated: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("detected_at", "created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def response_target(self) -> tuple[str, str]:
        from app.domain.enums import SEVERITY_RESPONSE_TARGETS

        return SEVERITY_RESPONSE_TARGETS[self.severity]


class Anomaly(ORMBackedModel):
    """A deterministic detection result — never an LLM judgement (OPS-030)."""

    id: str
    incident_id: str | None = None
    service: str
    metric: str
    family: MetricFamily
    baseline: float
    observed: float
    deviation: float
    z_score: float
    relative_deviation: float
    sample_count: int
    severity: Severity
    window_start: datetime
    window_end: datetime
    detected_at: datetime = Field(default_factory=utcnow)
    dedup_key: str
    deduplicated: bool = False
    simulated: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class DetectionResult(ORMBackedModel):
    """The detector's report for one metric series (the OPS-030 output contract)."""

    metric: str
    family: MetricFamily
    service: str
    baseline: float
    observed: float
    deviation: float
    z_score: float
    relative_deviation: float
    sample_count: int
    severity: Severity
    detected_at: datetime = Field(default_factory=utcnow)
    is_anomaly: bool
    outcome: str  # normal | anomaly | insufficient_data
    window_start: datetime
    window_end: datetime
    explanation: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


__all__ = ["Anomaly", "DetectionResult", "Incident"]
