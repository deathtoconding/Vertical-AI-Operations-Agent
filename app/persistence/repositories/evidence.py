"""Evidence repository with evidence-id allocation (OPS-040)."""

from __future__ import annotations

import hashlib
import json
import secrets

from sqlalchemy import func, select

from app.domain.evidence import Evidence, EvidenceDraft
from app.persistence.models.evidence import EvidenceRow
from app.persistence.repositories.base import BaseRepository

EVIDENCE_ID_PREFIX = "EV-"


def content_hash(content: dict[str, object]) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class EvidenceRepository(BaseRepository[EvidenceRow, Evidence]):
    row_class = EvidenceRow
    domain_class = Evidence

    def _new_evidence_id(self) -> str:
        """Human-addressable short id: easy to cite in a diagnosis, still collision-safe."""
        return f"{EVIDENCE_ID_PREFIX}{secrets.token_hex(4).upper()}"

    async def persist(self, incident_id: str, drafts: list[EvidenceDraft]) -> list[Evidence]:
        rows: list[EvidenceRow] = []
        for draft in drafts:
            row = EvidenceRow(
                id=self._new_evidence_id(),
                incident_id=incident_id,
                source=draft.source.value,
                kind=draft.kind.value,
                summary=draft.summary[:2000],
                content=draft.content,
                timestamp=draft.timestamp,
                confidence=draft.confidence.value,
                reliability=draft.reliability if draft.reliability is not None else 0.6,
                content_hash=content_hash(draft.content),
                truncated=bool(draft.content.get("truncated", False)),
                injections=list(draft.content.get("injections", []) or []),
                simulated=draft.simulated,
            )
            rows.append(row)
        for row in rows:
            self.session.add(row)
        await self.session.flush()
        for row in rows:
            await self.session.refresh(row)
        return self.to_domain_list(rows)

    async def get_many(self, evidence_ids: list[str]) -> list[Evidence]:
        if not evidence_ids:
            return []
        result = await self.session.execute(
            select(EvidenceRow).where(EvidenceRow.id.in_(evidence_ids))
        )
        return self.to_domain_list(list(result.scalars().all()))

    async def list_for_incident(
        self, incident_id: str, *, limit: int = 200, offset: int = 0
    ) -> list[Evidence]:
        result = await self.session.execute(
            select(EvidenceRow)
            .where(EvidenceRow.incident_id == incident_id)
            .order_by(EvidenceRow.timestamp)
            .limit(limit)
            .offset(offset)
        )
        return self.to_domain_list(list(result.scalars().all()))

    async def count_for_incident(self, incident_id: str) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(EvidenceRow)
            .where(EvidenceRow.incident_id == incident_id)
        )
        return int(result.scalar_one())

    async def exists_for_incident(self, incident_id: str, evidence_ids: list[str]) -> set[str]:
        """Which of ``evidence_ids`` genuinely belong to this incident?

        This is the grounding check: a diagnosis may only cite evidence that exists **and**
        belongs to the incident it is describing (OPS-041 acceptance criteria).
        """
        if not evidence_ids:
            return set()
        result = await self.session.execute(
            select(EvidenceRow.id).where(
                EvidenceRow.incident_id == incident_id, EvidenceRow.id.in_(evidence_ids)
            )
        )
        return {str(row_id) for row_id in result.scalars().all()}


__all__ = ["EVIDENCE_ID_PREFIX", "EvidenceRepository", "content_hash"]
