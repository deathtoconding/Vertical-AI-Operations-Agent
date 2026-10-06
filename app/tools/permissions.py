"""Tool -> permission mapping, and the autonomy policy that reads it.

Two ideas live here:

* **declared permissions** — a tool names exactly one permission, and that permission must
  exist in :class:`app.core.security.Permission`. A typo is a startup error, not a silent
  hole.
* **autonomy tiers** — how much a risk level may do at each :class:`AutonomyLevel`. This is
  the gate that keeps a critical action human-controlled no matter how confident the model
  is (section 39 V6 of the plan).
"""

from __future__ import annotations

from typing import Final

from app.core.security import Permission
from app.domain.enums import AutonomyLevel, RiskLevel, Role

#: The permission vocabulary, as strings, for validation at registration time.
PERMISSION_NAMES: Final[frozenset[str]] = frozenset(permission.value for permission in Permission)

#: Which roles may *hold* a permission. Derived from the matrix in ``app.core.security``
#: and asserted consistent by a unit test, so the two cannot drift.
TOOL_PERMISSIONS: Final[dict[str, Permission]] = {
    "knowledge.base_example": Permission.KNOWLEDGE_READ,
    "github.read_commits": Permission.GITHUB_READ,
    "metrics.query": Permission.METRICS_READ,
    "logs.query": Permission.LOGS_READ,
    "jira.create_incident": Permission.JIRA_WRITE,
    "slack.notify": Permission.SLACK_WRITE,
    "deployment.rollback_simulation": Permission.DEPLOYMENT_ROLLBACK,
    "incident.escalate": Permission.INCIDENTS_WRITE,
}

#: Autonomy behaviour per risk level. ``True`` means "may execute without a human decision".
AUTONOMY_MATRIX: Final[dict[AutonomyLevel, dict[RiskLevel, bool]]] = {
    AutonomyLevel.OBSERVE_ONLY: {
        RiskLevel.LOW: False,
        RiskLevel.MEDIUM: False,
        RiskLevel.HIGH: False,
        RiskLevel.CRITICAL: False,
    },
    AutonomyLevel.APPROVAL_REQUIRED: {
        RiskLevel.LOW: True,
        RiskLevel.MEDIUM: True,  # medium may execute, but the tool decides if approval is needed
        RiskLevel.HIGH: False,
        RiskLevel.CRITICAL: False,
    },
    AutonomyLevel.SELECTIVE_AUTONOMY: {
        RiskLevel.LOW: True,
        RiskLevel.MEDIUM: True,
        RiskLevel.HIGH: False,  # high risk is *never* automatic
        RiskLevel.CRITICAL: False,
    },
}

MINIMUM_ROLE_FOR_RISK: Final[dict[RiskLevel, Role]] = {
    RiskLevel.LOW: Role.VIEWER,
    RiskLevel.MEDIUM: Role.OPERATOR,
    RiskLevel.HIGH: Role.SRE,
    RiskLevel.CRITICAL: Role.ADMIN,
}


def may_execute_without_approval(autonomy: AutonomyLevel, risk: RiskLevel) -> bool:
    """True when the autonomy level permits a risk tier to act without a human decision."""
    return AUTONOMY_MATRIX[autonomy][risk]


def required_role(risk: RiskLevel) -> Role:
    return MINIMUM_ROLE_FOR_RISK[risk]


def role_satisfies(role: Role, risk: RiskLevel) -> bool:
    """True when ``role`` is at least the minimum role for ``risk``."""
    return role.rank >= MINIMUM_ROLE_FOR_RISK[risk].rank


__all__ = [
    "AUTONOMY_MATRIX",
    "MINIMUM_ROLE_FOR_RISK",
    "PERMISSION_NAMES",
    "TOOL_PERMISSIONS",
    "may_execute_without_approval",
    "required_role",
    "role_satisfies",
]
