"""Idempotency store (OPS-021, OPS-062).

Idempotency is what makes retries safe in a system that touches external systems: a second
attempt with the same key returns the first attempt's result instead of creating a duplicate
Jira issue, Slack message or rollback.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import IdempotencyConflict
from app.core.telemetry import IDEMPOTENT_REPLAYS
from app.persistence.models.audit import IdempotencyKeyRow
from app.persistence.models.base import utcnow


class IdempotencyStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, scope: str, key: str) -> IdempotencyKeyRow | None:
        result = await self.session.execute(
            select(IdempotencyKeyRow).where(
                IdempotencyKeyRow.scope == scope, IdempotencyKeyRow.key == key
            )
        )
        row = result.scalar_one_or_none()
        if row is not None and row.expires_at < utcnow():
            return None
        return row

    async def reserve(
        self,
        scope: str,
        key: str,
        request_hash: str,
        *,
        ttl_seconds: int = 86_400,
        external_id: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> tuple[IdempotencyKeyRow, bool]:
        """Reserve a key. Returns ``(row, created)``.

        ``created=False`` means this is a replay; the caller must return the stored result
        rather than performing the side effect again. A *different* request hash under the
        same key is a client bug and raises :class:`IdempotencyConflict`.
        """
        existing = await self.get(scope, key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyConflict(
                    "A different payload was already processed with this idempotency key.",
                    details={"scope": scope},
                )
            IDEMPOTENT_REPLAYS.labels(scope=scope).inc()
            return existing, False

        row = IdempotencyKeyRow(
            scope=scope,
            key=key,
            request_hash=request_hash,
            external_id=external_id,
            response=response or {},
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )
        self.session.add(row)
        await self.session.flush()
        return row, True

    async def complete(
        self, scope: str, key: str, *, external_id: str | None, response: dict[str, Any]
    ) -> None:
        row = await self.get(scope, key)
        if row is None:
            return
        row.external_id = external_id
        row.response = response
        await self.session.flush()

    async def purge_expired(self) -> int:
        from sqlalchemy import delete

        result = await self.session.execute(
            delete(IdempotencyKeyRow).where(IdempotencyKeyRow.expires_at < utcnow())
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


__all__ = ["IdempotencyStore"]
