"""The investigator (OPS-041).

Responsibilities, in order:

1. assemble the prompt context from persisted evidence (never from live system access);
2. ask the reasoner for a diagnosis;
3. **validate** it — every cited id must resolve to evidence that belongs to *this* incident;
4. **ground the confidence** — a diagnosis with no citations, or one built on flagged
   content, cannot be highly confident;
5. fall back to the deterministic reasoner if the LLM fails, labelling the result.

Step 3 is the reason a model cannot launder a hallucination into the audit trail: an
ungrounded citation is rejected outright, and the rejection itself is recorded.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from app.core.config import Settings
from app.core.errors import LLMUnavailable, UngroundedDiagnosis
from app.core.logging import get_logger
from app.core.telemetry import (
    DIAGNOSIS_GROUNDING_FAILURES,
    INVESTIGATION_LATENCY,
    REASONER_FALLBACK,
)
from app.domain.diagnosis import Diagnosis
from app.domain.enums import Confidence
from app.domain.evidence import Evidence
from app.domain.incidents import Incident
from app.investigation.context import build_investigation_prompt
from app.llm.provider import Reasoner, ReasoningRequest, build_reasoner

logger = get_logger(__name__)

MAX_CONFIDENCE_WITHOUT_DEPLOYMENT = 0.6
MAX_CONFIDENCE_WITH_FLAGGED_EVIDENCE = 0.65
MAX_CONFIDENCE_WITH_DEGRADED_SOURCES = 0.7


class Investigator:
    """Produce a grounded, schema-valid diagnosis for an incident."""

    def __init__(
        self,
        settings: Settings,
        *,
        reasoner: Reasoner | None = None,
        fallback: Reasoner | None = None,
    ) -> None:
        self.settings = settings
        self.reasoner = reasoner or build_reasoner(settings)
        from app.llm.provider import DeterministicReasoner

        self.fallback = fallback or DeterministicReasoner(degraded_reason="llm_unavailable")

    # ------------------------------------------------------------------ #

    async def investigate(
        self,
        incident: Incident,
        evidence: list[Evidence],
        *,
        anomaly: dict[str, Any] | None = None,
        degradations: list[dict[str, str]] | None = None,
    ) -> Diagnosis:
        started = perf_counter()
        system_prompt, user_prompt, metadata = build_investigation_prompt(
            incident,
            evidence,
            anomaly=anomaly,
            degradations=degradations,
            max_chars=self.settings.llm_max_prompt_chars,
        )
        request = ReasoningRequest(
            incident_id=incident.id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            evidence_ids=[item.id for item in evidence],
            metadata=metadata,
        )

        diagnosis = await self._reason(request, metadata)

        # --- grounding check ------------------------------------------------- #
        valid_ids = await self._validate_citations(incident, evidence, diagnosis)
        diagnosis = self._regrade(diagnosis, evidence, degradations or [], valid_ids)

        INVESTIGATION_LATENCY.observe(perf_counter() - started)
        logger.info(
            "investigation_completed",
            incident_id=incident.id,
            reasoner=diagnosis.reasoner,
            confidence=diagnosis.confidence,
            evidence_cited=len(diagnosis.evidence_ids),
            grounded=bool(valid_ids) or not diagnosis.evidence_ids,
            degraded=diagnosis.is_degraded,
            latency_seconds=round(perf_counter() - started, 4),
        )
        return diagnosis

    # ------------------------------------------------------------------ #

    async def _reason(self, request: ReasoningRequest, metadata: dict[str, Any]) -> Diagnosis:
        if self.reasoner.name == "deterministic":
            return await self.reasoner.diagnose(request)
        try:
            return await self.reasoner.diagnose(request)
        except LLMUnavailable as exc:
            REASONER_FALLBACK.labels(reason=exc.reason).inc()
            logger.warning("llm_unavailable_falling_back", reason=exc.reason)
            from app.llm.provider import DeterministicReasoner

            fallback = DeterministicReasoner(degraded_reason=f"llm_{exc.reason}")
            return await fallback.diagnose(request)

    async def _validate_citations(
        self, incident: Incident, evidence: list[Evidence], diagnosis: Diagnosis
    ) -> set[str]:
        """Return the cited ids that genuinely belong to this incident.

        Any cited id that does not resolve is removed and counted. If *every* citation is
        invalid, the diagnosis is rejected — that is not a partial success, it is a
        fabrication.
        """
        known = {item.id for item in evidence}
        cited = set(diagnosis.evidence_ids) | set(diagnosis.counter_evidence_ids)
        unknown = cited - known
        if unknown:
            DIAGNOSIS_GROUNDING_FAILURES.inc()
            logger.warning(
                "diagnosis_ungrounded",
                incident_id=incident.id,
                unknown_ids=sorted(unknown)[:10],
                reasoner=diagnosis.reasoner,
            )

        valid = cited & known
        if cited and not valid:
            raise UngroundedDiagnosis(
                "The diagnosis cited evidence that does not belong to this incident.",
                details={"unknown_ids": sorted(unknown)[:10], "model": diagnosis.reasoner},
            )

        return valid

    def _regrade(
        self,
        diagnosis: Diagnosis,
        evidence: list[Evidence],
        degradations: list[dict[str, str]],
        valid_ids: set[str],
    ) -> Diagnosis:
        """Adjust confidence downwards for reasons the model is not trusted to self-report.

        A model that claims 0.95 confidence while citing one metric series, from a degraded
        source set, with an injected instruction in the logs, is *wrong about its own
        reliability*. The system corrects that deterministically rather than accepting it.
        """
        confidence = diagnosis.confidence
        notes: list[str] = list(diagnosis.uncertainties)

        filtered_evidence = [item for item in diagnosis.evidence_ids if item in valid_ids]
        rejected = [item for item in diagnosis.evidence_ids if item not in valid_ids]
        counter_filtered = [item for item in diagnosis.counter_evidence_ids if item in valid_ids]

        if rejected:
            notes.append(
                "some cited evidence ids could not be resolved and were discarded: "
                + ", ".join(sorted(rejected)[:5])
            )
            confidence = min(confidence, 0.4)

        has_deployment = any(item.source.value in {"deployment", "github"} for item in evidence)
        if not has_deployment:
            confidence = min(confidence, MAX_CONFIDENCE_WITHOUT_DEPLOYMENT)
            notes.append("no deployment evidence was available to correlate with the onset")

        flagged = [item for item in evidence if item.injections]
        if flagged:
            confidence = min(confidence, MAX_CONFIDENCE_WITH_FLAGGED_EVIDENCE)
            notes.append(
                "some evidence contained instruction-like content and was treated as data only"
            )

        if degradations:
            confidence = min(confidence, MAX_CONFIDENCE_WITH_DEGRADED_SOURCES)
            notes.append(
                "evidence sources were degraded: "
                + ", ".join(sorted({item.get("source", "unknown") for item in degradations}))
            )

        if len(valid_ids) < 2:
            confidence = min(confidence, 0.5)
            notes.append("fewer than two evidence items support this conclusion")

        injection_flags = (
            sorted({flag for item in evidence for flag in item.injections})
            or diagnosis.injection_flags
        )

        return diagnosis.model_copy(
            update={
                "confidence": round(max(0.05, min(confidence, 0.95)), 3),
                "confidence_level": _level(confidence),
                "evidence_ids": filtered_evidence,
                "counter_evidence_ids": counter_filtered,
                "uncertainties": notes,
                "injection_flags": injection_flags,
            }
        )


def _level(confidence: float) -> Confidence:
    if confidence >= 0.75:
        return Confidence.HIGH
    if confidence >= 0.4:
        return Confidence.MEDIUM
    return Confidence.LOW


__all__ = [
    "MAX_CONFIDENCE_WITHOUT_DEPLOYMENT",
    "Investigator",
]
