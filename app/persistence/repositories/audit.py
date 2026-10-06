"""Append-only, hash-chained audit repository (SEC-004 / ADR-0009).

Two properties are structural rather than conventional:

* **Append-only** — this class exposes ``append`` and read methods. There is no update or
  delete method to forget to avoid, and ``tests/integration/test_audit.py`` asserts that.
* **Tamper-evident** — each entry's hash covers the previous entry's hash, so editing,
  deleting or reordering a record breaks :meth:`AuditRepository.verify_chain`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, redact_mapping
from app.core.telemetry import AUDIT_CHAIN_BROKEN, AUDIT_EVENTS
from app.domain.enums import AuditEventType
from app.persistence.models.audit import AuditEventRow
from app.persistence.models.base import new_id, utcnow

logger = get_logger(__name__)

GENESIS_HASH = "0" * 64


def compute_entry_hash(
    *, prev_hash: str, occurred_at: datetime, event_type: str, payload: dict[str, Any]
) -> str:
    """Hash of (previous hash, timestamp, type, payload) — the chain link."""
    canonical = json.dumps(
        {
            "prev": prev_hash,
            "at": occurred_at.isoformat(),
            "type": event_type,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class AuditRepository:
    """The only writer of ``audit_events``."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _chain_head(self) -> tuple[str, int]:
        result = await self.session.execute(
            select(AuditEventRow.entry_hash, AuditEventRow.seq)
            .order_by(AuditEventRow.seq.desc())
            .limit(1)
        )
        row = result.first()
        if row is None:
            return GENESIS_HASH, 0
        return str(row[0]), int(row[1])

    async def append(
        self,
        event_type: AuditEventType,
        *,
        actor: str,
        role: str = "system",
        incident_id: str | None = None,
        agent_run_id: str | None = None,
        tool_name: str | None = None,
        action_id: str | None = None,
        approval_id: str | None = None,
        outcome: str = "info",
        reason: str = "",
        payload: dict[str, Any] | None = None,
    ) -> AuditEventRow:
        """Append one audited event. Secrets in the payload are redacted before hashing."""
        prev_hash, _ = await self._chain_head()
        occurred_at = utcnow()
        safe_payload = redact_mapping(payload or {})
        entry_hash = compute_entry_hash(
            prev_hash=prev_hash,
            occurred_at=occurred_at,
            event_type=event_type.value,
            payload=safe_payload,
        )
        row = AuditEventRow(
            id=new_id(),
            event_type=event_type.value,
            actor=actor,
            role=role,
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            tool_name=tool_name,
            action_id=action_id,
            approval_id=approval_id,
            outcome=outcome,
            reason=reason[:4000],
            payload=safe_payload,
            occurred_at=occurred_at,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        self.session.add(row)
        await self.session.flush()
        AUDIT_EVENTS.labels(event_type=event_type.value).inc()
        return row

    async def list_events(
        self,
        *,
        incident_id: str | None = None,
        agent_run_id: str | None = None,
        actor: str | None = None,
        event_type: AuditEventType | None = None,
        since: datetime | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[AuditEventRow]:
        statement = select(AuditEventRow).order_by(AuditEventRow.seq.desc())
        if incident_id:
            statement = statement.where(AuditEventRow.incident_id == incident_id)
        if agent_run_id:
            statement = statement.where(AuditEventRow.agent_run_id == agent_run_id)
        if actor:
            statement = statement.where(AuditEventRow.actor == actor)
        if event_type:
            statement = statement.where(AuditEventRow.event_type == event_type.value)
        if since:
            statement = statement.where(AuditEventRow.occurred_at >= since)
        result = await self.session.execute(statement.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def iter_chain(self, limit: int = 10_000) -> list[AuditEventRow]:
        result = await self.session.execute(
            select(AuditEventRow).order_by(AuditEventRow.seq).limit(limit)
        )
        return list(result.scalars().all())

    async def verify_chain(self) -> dict[str, Any]:
        """Walk the chain and report the first divergence, if any."""
        rows = await self.iter_chain()
        prev = GENESIS_HASH
        for index, row in enumerate(rows):
            expected = compute_entry_hash(
                prev_hash=prev,
                occurred_at=row.occurred_at,
                event_type=row.event_type,
                payload=row.payload,
            )
            if row.prev_hash != prev or row.entry_hash != expected:
                AUDIT_CHAIN_BROKEN.set(1)
                logger.error(
                    "audit_chain_broken",
                    index=index,
                    seq=row.seq,
                    expected=expected,
                    actual=row.entry_hash,
                )
                return {
                    "valid": False,
                    "entries": len(rows),
                    "broken_at_seq": row.seq,
                    "expected_hash": expected,
                    "actual_hash": row.entry_hash,
                }
            prev = row.entry_hash
        AUDIT_CHAIN_BROKEN.set(0)
        return {"valid": True, "entries": len(rows), "head": prev}

    async def count(self) -> int:
        from sqlalchemy import func

        result = await self.session.execute(select(func.count()).select_from(AuditEventRow))
        return int(result.scalar_one())


__all__ = ["GENESIS_HASH", "AuditRepository", "compute_entry_hash"]
