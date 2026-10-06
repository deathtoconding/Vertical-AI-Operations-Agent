"""Diagnosis and hypothesis objects — the LLM's *only* output format (OPS-041).

The schema is the contract that makes the model safe to use: it can propose, cite and
qualify, but every field is validated before anything downstream sees it. A hypothesis that
cites evidence which does not exist is rejected outright (``UngroundedDiagnosis``), and
claims the evidence does not support must be listed in ``unsupported_claims`` rather than
quietly asserted.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from app.domain.base import DomainModel
from app.domain.enums import Confidence


class RecommendedAction(DomainModel):
    """A proposed tool call, expressed as *intent* — the planner resolves it to a tool."""

    intent: str = Field(
        description="Machine-readable intent, e.g. 'rollback_deployment', 'notify_slack'."
    )
    tool_hint: str | None = Field(
        default=None,
        description="Suggested registered tool name. Unknown names are dropped by the planner.",
    )
    params: dict[str, object] = Field(default_factory=dict)
    rationale: str = ""
    risk_hint: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Hypothesis(DomainModel):
    """A candidate explanation with explicit support and counter-evidence."""

    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    counter_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    causal_chain: list[str] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _round(cls, value: float) -> float:
        return round(value, 3)


class Diagnosis(DomainModel):
    """The full, schema-validated result of an investigation."""

    hypothesis: str
    evidence_ids: list[str] = Field(default_factory=list)
    counter_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    reasoner: str = "deterministic"
    degraded_reason: str | None = None
    injection_flags: list[str] = Field(default_factory=list)
    confidence_level: Confidence = Confidence.MEDIUM
    prompt_version: str = "v1"
    tokens_used: int = 0
    latency_seconds: float = 0.0

    @model_validator(mode="after")
    def _derive_confidence_level(self) -> Diagnosis:
        level = Confidence.HIGH if self.confidence >= 0.75 else Confidence.MEDIUM
        if self.confidence < 0.4:
            level = Confidence.LOW
        object.__setattr__(self, "confidence_level", level)
        return self

    @property
    def all_evidence_ids(self) -> list[str]:
        return list(dict.fromkeys([*self.evidence_ids, *self.counter_evidence_ids]))

    @property
    def is_degraded(self) -> bool:
        """True when the deterministic fallback produced this diagnosis (ADR-0006)."""
        return self.reasoner != "llm"


__all__ = ["Diagnosis", "Hypothesis", "RecommendedAction"]
