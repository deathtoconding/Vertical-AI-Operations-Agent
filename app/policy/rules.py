"""Individual policy rules.

Each rule is a small, named, independently testable predicate. The engine composes them in a
fixed order, which makes the *reason* for a denial deterministic and reportable — an operator
reading "denied by rule `blocked_tool_disabled`" needs no further archaeology.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.security import Actor, Permission
from app.domain.enums import AutonomyLevel, PolicyDecisionType, RiskLevel
from app.domain.incidents import Incident
from app.domain.tools import ToolSpec
from app.tools.permissions import may_execute_without_approval, role_satisfies


def _actor_has(actor: Actor, permission: str) -> bool:
    """Permission check by *name*, so tool specs (data) and the matrix (code) stay decoupled."""
    try:
        return actor.has(Permission(permission))
    except ValueError:
        # An unknown permission name cannot be granted; deny rather than crash.
        return False


@dataclass(frozen=True)
class RuleOutcome:
    """The verdict of a single rule, with the name that will appear in the audit trail."""

    rule: str
    decision: PolicyDecisionType
    reason: str
    details: dict[str, Any] | None = None


def rule_tool_disabled(
    spec: ToolSpec, *, disabled: frozenset[str] = frozenset(), **_: Any
) -> RuleOutcome | None:
    """An operator can always remove a capability without a deploy."""
    if spec.name in disabled or "*" in disabled:
        return RuleOutcome(
            rule="blocked_tool_disabled",
            decision=PolicyDecisionType.DENY,
            reason=f"Tool '{spec.name}' is administratively disabled.",
        )
    return None


def rule_actor_permission(spec: ToolSpec, *, actor: Actor, **_: Any) -> RuleOutcome | None:
    """The actor must actually hold the permission the tool declares."""
    if not actor.authenticated:
        return RuleOutcome(
            rule="actor_unauthenticated",
            decision=PolicyDecisionType.DENY,
            reason="The actor is not authenticated.",
        )
    if not _actor_has(actor, spec.permission):
        return RuleOutcome(
            rule="actor_lacks_permission",
            decision=PolicyDecisionType.DENY,
            reason=f"Role '{actor.role.value}' does not hold '{spec.permission}'.",
            details={"permission": spec.permission, "role": actor.role.value},
        )
    return None


def rule_role_risk_floor(
    spec: ToolSpec,
    *,
    actor: Actor,
    effective_risk: RiskLevel = RiskLevel.LOW,
    **_: Any,
) -> RuleOutcome | None:
    """A viewer may never trigger a high-risk effect, whatever else says otherwise."""
    if not role_satisfies(actor.role, effective_risk):
        return RuleOutcome(
            rule="role_below_risk_floor",
            decision=PolicyDecisionType.DENY,
            reason=(
                f"Role '{actor.role.value}' is below the minimum role for "
                f"{effective_risk.value} risk."
            ),
            details={"risk": effective_risk.value, "role": actor.role.value},
        )
    return None


def rule_autonomy(
    spec: ToolSpec, *, autonomy: AutonomyLevel = AutonomyLevel.APPROVAL_REQUIRED, **_: Any
) -> RuleOutcome | None:
    if autonomy is AutonomyLevel.OBSERVE_ONLY:
        return RuleOutcome(
            rule="autonomy_observe_only",
            decision=PolicyDecisionType.DENY,
            reason=(
                "The system is in observe_only mode: it may detect, investigate and propose, "
                "but not execute."
            ),
        )
    return None


def rule_incident_terminal(
    spec: ToolSpec, *, incident: Incident | None = None, **_: Any
) -> RuleOutcome | None:
    """No new actions against an incident that is already resolved or escalated."""
    if incident is not None and incident.status.is_terminal:
        return RuleOutcome(
            rule="incident_already_terminal",
            decision=PolicyDecisionType.DENY,
            reason=f"Incident is already {incident.status.value}; no further actions apply.",
        )
    return None


def rule_incident_scope(
    spec: ToolSpec,
    *,
    incident: Incident | None = None,
    params: dict[str, Any] | None = None,
    **_: Any,
) -> RuleOutcome | None:
    """An action may only touch the incident it was proposed for (threat T-03).

    This blocks the classic injected instruction "roll back the *other* service", and more
    importantly it blocks a confused planner from acting across incident boundaries.
    """
    claimed = (params or {}).get("incident_id")
    if incident is not None and claimed is not None and str(claimed) != incident.id:
        return RuleOutcome(
            rule="cross_incident_action",
            decision=PolicyDecisionType.DENY,
            reason="The action targets a different incident than the one under investigation.",
            details={"expected": incident.id, "claimed": str(claimed)},
        )
    return None


def rule_rollback_loop_guard(
    spec: ToolSpec,
    *,
    prior_rollbacks: int = 0,
    settings: Settings,
    **_: Any,
) -> RuleOutcome | None:
    """Stop the rollback loop: rollback -> incident re-fires -> rollback (threat T-11)."""
    if spec.name != "deployment.rollback_simulation":
        return None
    if prior_rollbacks >= settings.max_rollbacks_per_hour:
        return RuleOutcome(
            rule="rollback_loop_guard",
            decision=PolicyDecisionType.DENY,
            reason=(
                f"{prior_rollbacks} rollbacks already attempted within the last hour; "
                "escalating to a human instead of looping."
            ),
            details={"prior_rollbacks": prior_rollbacks, "limit": settings.max_rollbacks_per_hour},
        )
    return None


def rule_approval_requirement(
    spec: ToolSpec,
    *,
    autonomy: AutonomyLevel = AutonomyLevel.APPROVAL_REQUIRED,
    effective_risk: RiskLevel = RiskLevel.LOW,
    **_: Any,
) -> RuleOutcome | None:
    """High risk is never automatic; tool-declared approval is never bypassed."""
    if spec.is_high_risk or effective_risk.at_least(RiskLevel.HIGH):
        return RuleOutcome(
            rule="high_risk_requires_approval",
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason=(
                f"{effective_risk.value} risk actions require a recorded human decision "
                "(they are never automatic at any autonomy level)."
            ),
        )
    if spec.requires_approval:
        return RuleOutcome(
            rule="tool_requires_approval",
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason=f"Tool '{spec.name}' declares that human approval is required.",
        )
    if not may_execute_without_approval(autonomy, effective_risk):
        return RuleOutcome(
            rule="autonomy_requires_approval",
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason=(
                f"Autonomy level '{autonomy.value}' requires approval for "
                f"{effective_risk.value} risk actions."
            ),
        )
    return None


RULES = (
    rule_tool_disabled,
    rule_actor_permission,
    rule_incident_terminal,
    rule_incident_scope,
    rule_rollback_loop_guard,
    rule_role_risk_floor,
    rule_autonomy,
    rule_approval_requirement,
)

__all__ = [
    "RULES",
    "RuleOutcome",
    "rule_actor_permission",
    "rule_approval_requirement",
    "rule_autonomy",
    "rule_incident_scope",
    "rule_incident_terminal",
    "rule_role_risk_floor",
    "rule_rollback_loop_guard",
    "rule_tool_disabled",
]
