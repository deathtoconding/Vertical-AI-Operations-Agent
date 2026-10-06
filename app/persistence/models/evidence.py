"""Evidence table — normalised, source-labelled, sanitised facts (OPS-040)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.models.base import Base, TimestampMixin, utcnow

JSONType = JSONB().with_variant(__import__("sqlalchemy").JSON, "sqlite")


class EvidenceRow(TimestampMixin, Base):
    """Evidence addressed by a short human-readable id (``EV-XXXXXXXX``)."""

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    confidence: Mapped[str] = mapped_column(String(8), nullable=False, default="medium")
    reliability: Mapped[float] = mapped_column(Float, nullable=False, default=0.6)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    injections: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_evidence_incident_source", "incident_id", "source"),)


__all__ = ["EvidenceRow"]
