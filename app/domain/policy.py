"""Policy decisions (SEC-002 / ADR-0002).

Policy returns one of exactly three answers, with the reason attached. There is no
"maybe", and there is no path by which an LLM's opinion can produce an ``ALLOW``.
"""

from __future__ import annotations

from pydantic import Field

from app.domain.base import DomainModel
from app.domain.enums import PolicyDecisionType, RiskLevel


class PolicyDecision(DomainModel):
    """The deterministic verdict for one action attempt."""

    decision: PolicyDecisionType
    reason: str
    tool_name: str
    risk: RiskLevel
    permission: str
    actor: str
    role: str
    requires_approval: bool = False
    autonomy_level: str = ""
    violations: list[str] = Field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecisionType.ALLOW

    @property
    def denied(self) -> bool:
        return self.decision is PolicyDecisionType.DENY

    @property
    def needs_approval(self) -> bool:
        return self.decision is PolicyDecisionType.REQUIRE_APPROVAL


class RiskAssessment(DomainModel):
    """Risk classification for a concrete action instance (not just its tool)."""

    tool_risk: RiskLevel
    effective_risk: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    escalators: list[str] = Field(default_factory=list)


__all__ = ["PolicyDecision", "RiskAssessment"]
