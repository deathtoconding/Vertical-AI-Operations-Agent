"""Agent run, transition log and tool invocations (OPS-050, OPS-051)."""

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

from app.persistence.models.base import Base, IdMixin, utcnow

JSONType = JSONB().with_variant(__import__("sqlalchemy").JSON, "sqlite")


class AgentRunRow(IdMixin, Base):
    """One execution of the agent workflow for one incident.

    ``version`` is the optimistic-concurrency token: two workers cannot both advance the
    same run (docs/architecture/agent-runtime.md §3).
    """

    __tablename__ = "agent_runs"

    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    stage: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    autonomy_level: Mapped[str] = mapped_column(String(32), nullable=False)
    reasoner: Mapped[str | None] = mapped_column(String(16))
    degraded_reason: Mapped[str | None] = mapped_column(String(64))
    diagnosis: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (Index("ix_agent_runs_state_updated", "state", "updated_at"),)


class RunTransitionRow(IdMixin, Base):
    """Append-only transition log. Every state change is auditable (OPS-002)."""

    __tablename__ = "run_transitions"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    incident_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_state: Mapped[str] = mapped_column(String(24), nullable=False)
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )


class ToolInvocationRow(IdMixin, Base):
    """Every attempt to use a tool, including denied and unknown-tool attempts."""

    __tablename__ = "tool_invocations"

    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    incident_id: Mapped[str | None] = mapped_column(String(64), index=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    request: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    response: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )


__all__ = ["AgentRunRow", "RunTransitionRow", "ToolInvocationRow"]
