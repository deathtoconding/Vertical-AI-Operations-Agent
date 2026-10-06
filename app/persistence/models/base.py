"""Shared ORM plumbing.

Enumerations are stored as plain strings: the closed vocabulary lives in
``app/domain/enums.py`` and is enforced when a domain model is validated, which keeps the
database schema portable and avoids a migration every time a state is added (states are
added by *code* review, not by DDL).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""

    #: Metadata naming convention so Alembic autogenerate produces stable constraint names.
    #: Declared as a ClassVar because SQLAlchemy reads it as configuration, not instance state.
    __table_args__: ClassVar[dict[str, bool]] = {"sqlite_autoincrement": False}


def utcnow() -> datetime:
    """Timezone-aware UTC now — every timestamp in this system is UTC (OPS-023)."""
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def new_prefixed_id(prefix: str) -> str:
    """A readable, namespaced id such as ``INC-1f0c9a2b3d4e5f60``.

    Readable ids matter in an incident: an operator reading a Slack message, a log line and an
    audit record can tell an incident from an action at a glance, and grep for one.
    """
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class IdMixin:
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)


def json_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


__all__ = ["IdMixin", "TimestampMixin", "json_dict", "new_id", "new_prefixed_id", "utcnow"]
