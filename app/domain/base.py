"""Domain model base.

The domain layer is deliberately dependency-free: pydantic for validation, and nothing
else. No FastAPI, no SQLAlchemy, no HTTP client, no LLM SDK. That boundary is what makes
the core logic testable in milliseconds and is enforced by an architecture test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer


class DomainModel(BaseModel):
    """Base for every domain object.

    ``from_attributes`` lets repositories build domain objects straight from ORM rows
    (``Model.model_validate(row)``) without a hand-written mapper per table.
    """

    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
        ser_json_timedelta="float",
    )


class ORMBackedModel(DomainModel):
    """A domain model that maps 1:1 onto a row.

    ``extra="ignore"`` is used here because ORM rows carry instrumentation attributes that
    are not part of the domain.
    """

    model_config = ConfigDict(
        from_attributes=True,
        extra="ignore",
        validate_assignment=True,
        use_enum_values=False,
    )

    @field_serializer("*", when_used="json", check_fields=False)
    def _serialise_utc(self, value: Any) -> Any:  # pragma: no cover - passthrough
        return value


def ensure_utc(value: datetime) -> datetime:
    """Normalise any datetime to timezone-aware UTC.

    Timezone bugs in an incident timeline are indistinguishable from lying, so timestamps are
    normalised at every boundary rather than trusted.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = ["DomainModel", "ORMBackedModel", "ensure_utc", "utcnow"]
