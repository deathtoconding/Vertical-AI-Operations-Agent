"""Evidence domain objects (OPS-040)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.domain.base import ORMBackedModel, ensure_utc, utcnow
from app.domain.enums import Confidence, EvidenceKind, EvidenceSource, confidence_score


class Evidence(ORMBackedModel):
    """A normalised, timestamped, source-labelled fact attached to an incident."""

    id: str
    incident_id: str
    source: EvidenceSource
    kind: EvidenceKind
    summary: str
    content: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)
    confidence: Confidence = Confidence.MEDIUM
    reliability: float = 0.6
    content_hash: str = ""
    truncated: bool = False
    injections: list[str] = Field(default_factory=list)
    simulated: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("timestamp", "created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @property
    def suspicious(self) -> bool:
        """True when this evidence matched an injection heuristic (SEC-003)."""
        return bool(self.injections)

    def rank_key(self) -> tuple[float, float]:
        """Ranking tuple: reliability first, then trust (unflagged before flagged).

        Flagged evidence is *shown* to the reasoner — hiding it would hide an attack — but it
        sorts below clean evidence of equal reliability.
        """
        trust = 0.0 if self.suspicious else 1.0
        return (self.reliability * (0.5 + 0.5 * trust), self.timestamp.timestamp())


class EvidenceDraft(ORMBackedModel):
    """Pre-persistence evidence produced by a collector.

    The collector produces drafts; the repository assigns the ``EV-…`` id and persists.
    """

    source: EvidenceSource
    kind: EvidenceKind
    summary: str
    content: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)
    confidence: Confidence = Confidence.MEDIUM
    reliability: float | None = None
    simulated: bool = False

    def model_post_init(self, __context: Any) -> None:
        if self.reliability is None:
            object.__setattr__(self, "reliability", confidence_score(self.confidence))


class EvidenceCollectionReport(ORMBackedModel):
    """Outcome of a fan-out collection: what arrived and what degraded (OPS-040 AC)."""

    incident_id: str
    #: Drafts produced by the collector, before the repository assigns ``EV-…`` ids.
    drafts: list[EvidenceDraft] = Field(default_factory=list)
    collected: list[Evidence] = Field(default_factory=list)
    degradations: list[dict[str, str]] = Field(default_factory=list)
    sources_queried: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def degraded(self) -> bool:
        return bool(self.degradations)

    def evidence_ids(self) -> list[str]:
        return [item.id for item in self.collected]


__all__ = ["Evidence", "EvidenceCollectionReport", "EvidenceDraft"]
