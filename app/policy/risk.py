"""Risk classification for concrete action instances (SEC-002 / ADR-0003).

A tool's declared risk is its *floor*, not its final score. The same rollback is a different
proposition during a quiet SEV3 and during a SEV1 with a second incident already open, so the
classifier can escalate risk with reasons — and every escalation is recorded, because
"why did this need approval?" must be answerable later.
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import RiskLevel, Severity
from app.domain.incidents import Incident
from app.domain.policy import RiskAssessment
from app.domain.tools import ToolSpec


def classify(
    spec: ToolSpec,
    *,
    incident: Incident | None = None,
    context: dict[str, Any] | None = None,
) -> RiskAssessment:
    """Return the effective risk for one action instance.

    Escalators implemented here:

    * an action during a **SEV1** is at least HIGH (blast radius is not the same as in a
      quiet window);
    * a **repeat** high-risk action within an incident is CRITICAL, because the second
      rollback of the same incident means the first diagnosis was wrong;
    * a **destructive parameter** (``force``, ``skip_verification``) escalates one tier —
      these are not accepted by any MVP tool, but the classifier must not assume that
      forever.
    """
    context = context or {}
    effective = spec.risk
    escalators: list[str] = []
    reasons = [f"tool '{spec.name}' declares risk {spec.risk.value}"]

    if incident is not None:
        if incident.severity is Severity.SEV1 and effective.at_least(RiskLevel.MEDIUM):
            effective = _raise_to(effective, RiskLevel.HIGH)
            escalators.append("severity_sev1")
            reasons.append("incident is SEV1, so any medium-or-higher action is at least HIGH")

        prior_rollbacks = int(context.get("prior_rollbacks", 0))
        if spec.risk.at_least(RiskLevel.HIGH) and prior_rollbacks >= 1:
            effective = RiskLevel.CRITICAL
            escalators.append("repeat_high_risk_action")
            reasons.append(f"{prior_rollbacks} high-risk action(s) already taken on this incident")

    params = context.get("params") or {}
    for flag in ("force", "skip_verification", "no_approval"):
        if params.get(flag):
            effective = _raise_to(effective, RiskLevel.HIGH)
            escalators.append(f"destructive_parameter:{flag}")
            reasons.append(f"parameter '{flag}' escalates risk")

    return RiskAssessment(
        tool_risk=spec.risk,
        effective_risk=effective,
        reasons=reasons,
        escalators=escalators,
    )


def _raise_to(current: RiskLevel, floor: RiskLevel) -> RiskLevel:
    return floor if current.rank < floor.rank else current


__all__ = ["classify"]
