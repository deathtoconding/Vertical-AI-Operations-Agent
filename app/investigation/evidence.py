"""Evidence normalisation, ranking and persistence mapping (OPS-040).

Three jobs, all of them about making evidence *usable and honest*:

* **Ranking** — reliability, then recency, then injection status. An attacker who can write log
  lines must not be able to push their content to the top of the model's context.
* **Normalisation** — every draft becomes a bounded, hashed, flagged record. The content hash is
  what makes tampering after the fact detectable.
* **Mapping** — turning drafts into domain evidence without a database, which is what lets the
  investigation layer be unit-tested without PostgreSQL.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.domain.enums import Confidence
from app.domain.evidence import Evidence, EvidenceDraft

#: Reliability weight per confidence level. Kept small and explicit so ranking is explainable.
CONFIDENCE_WEIGHTS: dict[Confidence, float] = {
    Confidence.HIGH: 1.0,
    Confidence.MEDIUM: 0.7,
    Confidence.LOW: 0.4,
}


def confidence_score(confidence: Confidence) -> float:
    return CONFIDENCE_WEIGHTS.get(confidence, 0.5)


def content_hash(content: dict[str, Any]) -> str:
    """Stable hash of the payload: sorted keys, no whitespace, strings for everything."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def rank_evidence(items: list[Evidence]) -> list[Evidence]:
    """Most trustworthy first: reliability, then recency, with flagged items demoted."""
    return sorted(items, key=lambda item: item.rank_key(), reverse=True)


def select_evidence(items: list[Evidence], limit: int) -> list[Evidence]:
    return rank_evidence(items)[:limit]


def to_domain_evidence(
    drafts: list[EvidenceDraft], *, incident_id: str, id_prefix: str = "EV"
) -> list[Evidence]:
    """Map drafts onto :class:`Evidence` objects with synthetic ids.

    Used for dry runs and unit tests. In production the repository allocates ids inside the
    transaction, because the id is part of the durable record.
    """
    evidence: list[Evidence] = []
    for index, draft in enumerate(drafts):
        content = dict(draft.content)
        evidence.append(
            Evidence(
                id=f"{id_prefix}-{index:04X}",
                incident_id=incident_id,
                source=draft.source,
                kind=draft.kind,
                summary=draft.summary,
                content=content,
                timestamp=draft.timestamp,
                confidence=draft.confidence,
                reliability=(
                    draft.reliability
                    if draft.reliability is not None
                    else confidence_score(draft.confidence)
                ),
                content_hash=content_hash(content),
                injections=list(content.get("injections") or []),
                simulated=draft.simulated,
            )
        )
    return evidence


def summarise_sources(items: list[Evidence]) -> dict[str, int]:
    """Counts per source — what the UI shows as "what we actually looked at"."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item.source.value] = counts.get(item.source.value, 0) + 1
    return counts


__all__ = [
    "CONFIDENCE_WEIGHTS",
    "confidence_score",
    "content_hash",
    "rank_evidence",
    "select_evidence",
    "summarise_sources",
    "to_domain_evidence",
]
