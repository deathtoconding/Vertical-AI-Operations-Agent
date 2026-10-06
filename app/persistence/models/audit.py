"""Append-only, hash-chained audit trail (SEC-004 / ADR-0009)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.models.base import Base, IdMixin, new_id, utcnow

JSONType = JSONB().with_variant(__import__("sqlalchemy").JSON, "sqlite")


class AuditEventRow(Base):
    """One consequential (or refused) step.

    ``seq`` exists to give the chain an unambiguous order; ``prev_hash``/``entry_hash`` make
    silent edits detectable.
    """

    __tablename__ = "audit_events"

    #: Monotonic primary key. The audit chain is ordered by ``seq``, and an append-only
    #: table wants a sequence PK rather than a random uuid (readable ordering, no
    #: page-splitting on insert).
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, default=new_id)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="system")
    incident_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    tool_name: Mapped[str | None] = mapped_column(String(64))
    action_id: Mapped[str | None] = mapped_column(String(64))
    approval_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (
        Index("ix_audit_incident_occurred", "incident_id", "occurred_at"),
        Index("ix_audit_event_type_occurred", "event_type", "occurred_at"),
    )


class IdempotencyKeyRow(IdMixin, Base):
    """Replay protection for actions and integration writes (OPS-062, OPS-021)."""

    __tablename__ = "idempotency_keys"

    scope: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120))
    response: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_idempotency_scope_key", "scope", "key", unique=True),)


__all__ = ["AuditEventRow", "IdempotencyKeyRow"]
