"""Verification domain objects (OPS-063 / ADR-0004)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.domain.base import DomainModel, ORMBackedModel, ensure_utc, utcnow
from app.domain.enums import VerificationOutcome


class CheckResult(DomainModel):
    """One check's observation and verdict, with the raw numbers kept for audit."""

    check: str
    outcome: VerificationOutcome
    observed: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    samples: int = 0
    source: str = ""

    @property
    def conclusive(self) -> bool:
        return self.outcome is not VerificationOutcome.UNKNOWN


class VerificationResult(ORMBackedModel):
    """The independent verdict for an action or an incident.

    Aggregation is deliberately pessimistic: any ``FAILED`` makes the whole result ``FAILED``,
    and an inconclusive run of checks yields ``UNKNOWN`` — never ``SUCCESS`` by default.
    """

    id: str
    incident_id: str
    action_id: str | None = None
    run_id: str | None = None
    outcome: VerificationOutcome
    reason: str = ""
    checks: list[dict[str, Any]] = Field(default_factory=list)
    observed: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime = Field(default_factory=utcnow)
    duration_seconds: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("started_at", "completed_at", "created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _utc_end(self) -> VerificationResult:
        return self

    @property
    def resolves_incident(self) -> bool:
        return self.outcome.resolves_incident


def aggregate_outcomes(results: list[CheckResult]) -> VerificationOutcome:
    """Combine check results into a single honest verdict.

    Rules:
        * no checks at all -> ``UNKNOWN`` (we cannot claim success we did not observe);
        * any ``FAILED`` -> ``FAILED`` (a contradiction outranks an inconclusive check);
        * any ``UNKNOWN`` (and no failure) -> ``UNKNOWN``;
        * otherwise -> ``SUCCESS``.
    """
    if not results:
        return VerificationOutcome.UNKNOWN
    outcomes = {result.outcome for result in results}
    if VerificationOutcome.FAILED in outcomes:
        return VerificationOutcome.FAILED
    if VerificationOutcome.UNKNOWN in outcomes:
        return VerificationOutcome.UNKNOWN
    return VerificationOutcome.SUCCESS


__all__ = ["CheckResult", "VerificationResult", "aggregate_outcomes"]
