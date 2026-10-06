"""Incident and anomaly tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.models.base import Base, IdMixin, TimestampMixin, utcnow

JSONType = JSONB().with_variant(__import__("sqlalchemy").JSON, "sqlite")


class IncidentRow(IdMixin, TimestampMixin, Base):
    """The durable record of one operational problem (domain: ``app.domain.incidents``)."""

    __tablename__ = "incidents"

    incident_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    service: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    metric: Mapped[str | None] = mapped_column(String(64))
    dedup_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_run_id: Mapped[str | None] = mapped_column(String(64))
    diagnosis: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    verification_outcome: Mapped[str | None] = mapped_column(String(16))
    escalation_reason: Mapped[str | None] = mapped_column(Text)
    jira_issue_key: Mapped[str | None] = mapped_column(String(64))
    slack_message_ts: Mapped[str | None] = mapped_column(String(64))
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rollback_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_incidents_status_severity_created", "status", "severity", "created_at"),
        Index("ix_incidents_created_at", "created_at"),
    )


class AnomalyRow(IdMixin, Base):
    """A detection event. Always persisted and linked, even when deduplicated (OPS-031)."""

    __tablename__ = "anomalies"

    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), index=True
    )
    service: Mapped[str] = mapped_column(String(120), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    family: Mapped[str] = mapped_column(String(32), nullable=False)
    baseline: Mapped[float] = mapped_column(Float, nullable=False)
    observed: Mapped[float] = mapped_column(Float, nullable=False)
    deviation: Mapped[float] = mapped_column(Float, nullable=False)
    z_score: Mapped[float] = mapped_column(Float, nullable=False)
    relative_deviation: Mapped[float] = mapped_column(Float, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    dedup_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    deduplicated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)


__all__ = ["AnomalyRow", "IncidentRow"]
