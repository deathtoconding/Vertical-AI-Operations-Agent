"""Incident and anomaly repositories."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import IncidentStatus, Severity
from app.domain.incidents import Anomaly, Incident
from app.persistence.models.base import utcnow
from app.persistence.models.incident import AnomalyRow, IncidentRow
from app.persistence.repositories.base import BaseRepository

#: Explicit alias: the class below defines a ``list`` method, which shadows the builtin inside
#: the class body and would otherwise make ``list[Incident]`` in its annotations unresolvable.
IncidentList = list[Incident]


class IncidentRepository(BaseRepository[IncidentRow, Incident]):
    row_class = IncidentRow
    domain_class = Incident

    async def get_by_dedup_key(self, dedup_key: str) -> Incident | None:
        """Exact lookup of the row that owns a bucket key."""
        result = await self.session.execute(
            select(IncidentRow).where(IncidentRow.dedup_key == dedup_key)
        )
        row = result.scalar_one_or_none()
        return self.to_domain(row) if row else None

    async def get_active_by_dedup_key(self, dedup_key: str) -> Incident | None:
        """The *active* incident for a detection bucket, if one exists.

        Deduplication is about not opening a second incident while one is still being worked on.
        A finished incident must not swallow the next detection: once an incident is resolved (or
        escalated to a human), the same deviation recurring is new information — a rollback that
        did not hold, a fix that did not ship — and it deserves its own incident instead of being
        counted as a recurrence of a closed one.

        Lookup is by key *prefix* because a bucket key is reused after the incident that owned it
        reached a terminal state; the later incident carries a derived key (see
        :mod:`app.application.incident_service`) so the unique constraint still holds.
        """
        terminal = sorted(status.value for status in IncidentStatus if status.is_terminal)
        result = await self.session.execute(
            select(IncidentRow)
            .where(IncidentRow.dedup_key.like(f"{dedup_key}%"))
            .where(IncidentRow.status.notin_(terminal))
            .order_by(IncidentRow.detected_at.desc())
        )
        row = result.scalars().first()
        return self.to_domain(row) if row else None

    async def create(self, values: dict[str, Any]) -> Incident:
        row = IncidentRow(**values)
        await self.add(row)
        await self.session.refresh(row)
        return self.to_domain(row)

    async def update(self, incident_id: str, values: dict[str, Any]) -> Incident | None:
        row = await self.get_row(incident_id)
        if row is None:
            return None
        self.apply(row, {"updated_at": utcnow(), **values})
        await self.session.flush()
        await self.session.refresh(row)
        return self.to_domain(row)

    async def list(
        self,
        *,
        status: IncidentStatus | None = None,
        severity: Severity | None = None,
        service: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Incident]:
        statement = select(IncidentRow).order_by(IncidentRow.created_at.desc())
        if status is not None:
            statement = statement.where(IncidentRow.status == status.value)
        if severity is not None:
            statement = statement.where(IncidentRow.severity == severity.value)
        if service is not None:
            statement = statement.where(IncidentRow.service == service)
        statement = statement.limit(limit).offset(offset)
        result = await self.session.execute(statement)
        return self.to_domain_list(list(result.scalars().all()))

    async def list_open(self, limit: int = 100) -> IncidentList:
        result = await self.session.execute(
            select(IncidentRow)
            .where(
                IncidentRow.status.in_(
                    [IncidentStatus.OPEN.value, IncidentStatus.ACKNOWLEDGED.value]
                )
            )
            .order_by(IncidentRow.created_at.desc())
            .limit(limit)
        )
        return self.to_domain_list(list(result.scalars().all()))

    async def count_by_status(self) -> dict[str, int]:
        result = await self.session.execute(
            select(IncidentRow.status, func.count()).group_by(IncidentRow.status)
        )
        return {str(status): int(count) for status, count in result.all()}

    async def count_open_by_severity(self) -> dict[Severity, int]:
        result = await self.session.execute(
            select(IncidentRow.severity, func.count())
            .where(
                IncidentRow.status.in_(
                    [IncidentStatus.OPEN.value, IncidentStatus.ACKNOWLEDGED.value]
                )
            )
            .group_by(IncidentRow.severity)
        )
        counts = dict.fromkeys(Severity, 0)
        for severity_value, count in result.all():
            counts[Severity(str(severity_value))] = int(count)
        return counts

    async def count_rollbacks(self, incident_id: str) -> int:
        row = await self.get_row(incident_id)
        return int(row.rollback_count) if row else 0

    async def list_recent(
        self, *, since: datetime, service: str | None = None, limit: int = 50
    ) -> IncidentList:
        statement = (
            select(IncidentRow)
            .where(IncidentRow.detected_at >= since)
            .order_by(IncidentRow.detected_at.desc())
            .limit(limit)
        )
        if service:
            statement = statement.where(IncidentRow.service == service)
        result = await self.session.execute(statement)
        return self.to_domain_list(list(result.scalars().all()))

    async def list_stale_open(self, older_than_minutes: int = 1440) -> IncidentList:
        """Open incidents that should have reached a terminal state by now.

        Backs the "lost critical incidents" SLI: an incident that never resolves or escalates
        is a product defect, not a state to be tolerated.
        """
        cutoff = utcnow() - timedelta(minutes=older_than_minutes)
        result = await self.session.execute(
            select(IncidentRow).where(
                IncidentRow.status.in_(
                    [IncidentStatus.OPEN.value, IncidentStatus.ACKNOWLEDGED.value]
                ),
                IncidentRow.created_at < cutoff,
            )
        )
        return self.to_domain_list(list(result.scalars().all()))


class AnomalyRepository(BaseRepository[AnomalyRow, Anomaly]):
    row_class = AnomalyRow
    domain_class = Anomaly

    async def create(self, values: dict[str, Any]) -> Anomaly:
        row = AnomalyRow(**values)
        await self.add(row)
        await self.session.refresh(row)
        return self.to_domain(row)

    async def list_for_incident(self, incident_id: str) -> list[Anomaly]:
        result = await self.session.execute(
            select(AnomalyRow)
            .where(AnomalyRow.incident_id == incident_id)
            .order_by(AnomalyRow.detected_at)
        )
        return self.to_domain_list(list(result.scalars().all()))

    async def recent_dedup_keys(self, since: datetime) -> set[str]:
        result = await self.session.execute(
            select(AnomalyRow.dedup_key).where(AnomalyRow.detected_at >= since)
        )
        return {str(key) for key in result.scalars().all()}


def build_incident_repository(session: AsyncSession) -> IncidentRepository:
    return IncidentRepository(session)


__all__ = ["AnomalyRepository", "IncidentRepository", "build_incident_repository"]
