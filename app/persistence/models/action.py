"""Action, approval and verification tables (EPIC-07)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.models.base import Base, IdMixin, TimestampMixin, utcnow

JSONType = JSONB().with_variant(__import__("sqlalchemy").JSON, "sqlite")


class ActionRow(IdMixin, TimestampMixin, Base):
    """A proposed or executed tool call.

    ``canonical_hash`` hashes the exact payload that policy evaluated and a human approved;
    any change invalidates the approval (threat T-09).
    """

    __tablename__ = "actions"

    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    permission: Mapped[str] = mapped_column(String(48), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_ids: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    expected_state: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    policy_decision: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    policy_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_actions_incident_status", "incident_id", "status"),)


class ApprovalRow(IdMixin, Base):
    """A human decision bound to one exact action payload."""

    __tablename__ = "approvals"

    action_id: Mapped[str] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    incident_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationRow(IdMixin, TimestampMixin, Base):
    """The independent verdict. Never derived from the executor's response."""

    __tablename__ = "verifications"

    incident_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action_id: Mapped[str | None] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)
    observed: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


__all__ = ["ActionRow", "ApprovalRow", "VerificationRow"]
