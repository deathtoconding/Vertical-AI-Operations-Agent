"""Authorization helpers shared by the API and the executor (SEC-002).

Route guards and execution guards must agree, or a viewer could trigger through the agent
what they cannot trigger through the API. Both call these functions; nothing else decides
permissions.
"""

from __future__ import annotations

from app.core.errors import AuthorizationError
from app.core.security import Actor, Permission
from app.domain.enums import RiskLevel
from app.tools.permissions import required_role, role_satisfies


def require(actor: Actor, permission: Permission) -> None:
    """Raise 403 (and count it) unless the actor holds ``permission``."""
    actor.require(permission)


def can_execute(actor: Actor, permission: Permission, risk: RiskLevel) -> bool:
    """True when the actor may execute a tool of this risk level."""
    return actor.has(permission) and role_satisfies(actor.role, risk)


def require_execution(actor: Actor, permission: Permission, risk: RiskLevel) -> None:
    if not actor.has(permission):
        raise AuthorizationError(
            f"Role '{actor.role.value}' cannot execute actions requiring '{permission.value}'.",
            details={"permission": permission.value},
        )
    if not role_satisfies(actor.role, risk):
        raise AuthorizationError(
            f"{risk.value} risk actions require role '{required_role(risk).value}' or higher.",
            details={"risk": risk.value, "role": actor.role.value},
        )


__all__ = ["can_execute", "require", "require_execution"]
