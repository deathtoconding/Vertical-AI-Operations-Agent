"""Authentication, authorization and the permission matrix (SEC-002).

Least privilege is expressed as data, not as prose:

* **one role per token**, deny by default;
* a **permission matrix** (role → permissions) that the API guards and the tool executor
  both consult — so a route guard and a tool check cannot disagree;
* **constant-time** token comparison on hashes, so token validity is not leaked by timing;
* every denial is audited and counted. `403` is not an error, it is a recorded decision.

The permission model is deliberately *coarse* and *closed*. An unknown permission cannot
be granted, and there is no "superuser bypass" other than the explicit ``admin`` role —
which is itself audited.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.config import Settings, TokenBinding
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.telemetry import AUTH_FAILURES, AUTHORIZATION_DENIALS
from app.domain.enums import Role


class Permission(StrEnum):
    """Closed permission vocabulary. Every guarded capability names one of these."""

    # read
    INCIDENTS_READ = "incidents.read"
    EVIDENCE_READ = "evidence.read"
    AGENT_RUNS_READ = "agent_runs.read"
    ACTIONS_READ = "actions.read"
    AUDIT_READ = "audit.read"
    TOOLS_READ = "tools.read"
    METRICS_READ = "metrics.read"
    LOGS_READ = "logs.read"
    GITHUB_READ = "github.read"
    KNOWLEDGE_READ = "knowledge.read"
    # write
    INCIDENTS_WRITE = "incidents.write"
    EVIDENCE_COLLECT = "evidence.collect"
    AGENT_RUN = "agents.run"
    JIRA_WRITE = "jira.write"
    SLACK_WRITE = "slack.write"
    DEPLOYMENT_ROLLBACK = "deployment.rollback"
    APPROVALS_DECIDE = "approvals.decide"
    ADMIN_AUTONOMY = "admin.autonomy"
    ADMIN_RECOVERY = "admin.recovery"


READ_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.INCIDENTS_READ,
        Permission.EVIDENCE_READ,
        Permission.AGENT_RUNS_READ,
        Permission.ACTIONS_READ,
        Permission.AUDIT_READ,
        Permission.TOOLS_READ,
    }
)

#: The matrix. Read this as the contract; `tests_unit/core` asserts every role's contents.
ROLE_PERMISSIONS: Final[dict[Role, frozenset[Permission]]] = {
    Role.VIEWER: READ_PERMISSIONS,
    Role.OPERATOR: READ_PERMISSIONS
    | frozenset(
        {
            Permission.INCIDENTS_WRITE,
            Permission.EVIDENCE_COLLECT,
            Permission.AGENT_RUN,
            Permission.APPROVALS_DECIDE,
            Permission.JIRA_WRITE,
            Permission.SLACK_WRITE,
            Permission.METRICS_READ,
            Permission.LOGS_READ,
            Permission.GITHUB_READ,
            Permission.KNOWLEDGE_READ,
        }
    ),
    Role.SRE: READ_PERMISSIONS
    | frozenset(
        {
            Permission.INCIDENTS_WRITE,
            Permission.EVIDENCE_COLLECT,
            Permission.AGENT_RUN,
            Permission.APPROVALS_DECIDE,
            Permission.JIRA_WRITE,
            Permission.SLACK_WRITE,
            Permission.METRICS_READ,
            Permission.LOGS_READ,
            Permission.GITHUB_READ,
            Permission.KNOWLEDGE_READ,
            Permission.DEPLOYMENT_ROLLBACK,
            Permission.ADMIN_RECOVERY,
        }
    ),
    Role.ADMIN: frozenset(Permission),
}


@dataclass(frozen=True)
class Actor:
    """An authenticated principal. ``system`` is the agent itself, never a human."""

    actor_id: str
    role: Role
    authenticated: bool = True

    @property
    def is_system(self) -> bool:
        return self.actor_id == "system"

    @classmethod
    def system(cls, role: Role = Role.SRE) -> Actor:
        return cls(actor_id="system", role=role, authenticated=True)

    @classmethod
    def anonymous(cls) -> Actor:
        return cls(actor_id="anonymous", role=Role.VIEWER, authenticated=False)

    def permissions(self) -> frozenset[Permission]:
        return ROLE_PERMISSIONS.get(self.role, frozenset())

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions()

    def require(self, permission: Permission) -> None:
        """Raise :class:`AuthorizationError` unless this actor holds ``permission``."""
        if not self.authenticated:
            raise AuthenticationError("Authentication is required for this operation.")
        if not self.has(permission):
            AUTHORIZATION_DENIALS.labels(permission=permission.value, role=self.role.value).inc()
            raise AuthorizationError(
                f"Role '{self.role.value}' does not hold permission '{permission.value}'.",
                details={"permission": permission.value, "role": self.role.value},
            )


def authenticate(authorization_header: str | None, settings: Settings) -> Actor:
    """Validate a ``Bearer`` token against the configured token set.

    Uses ``hmac.compare_digest`` over SHA-256 digests so neither the token content nor the
    number of matching characters can be observed from response timing.
    """
    if not authorization_header:
        AUTH_FAILURES.labels(reason="missing_token").inc()
        if not settings.tokens:
            # Development convenience: no tokens configured means the configured default
            # role is assumed, and this is impossible outside development because Settings
            # refuses to build in staging/production without tokens.
            return Actor(actor_id="dev", role=settings.default_actor_role)
        raise AuthenticationError("Authorization header is required.")

    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        AUTH_FAILURES.labels(reason="invalid_token").inc()
        raise AuthenticationError("Authorization header must be 'Bearer <token>'.")

    registry = settings.tokens
    if not registry:
        AUTH_FAILURES.labels(reason="invalid_token").inc()
        raise AuthenticationError("No API tokens are configured.")

    digest = __import__("hashlib").sha256(token.strip().encode()).hexdigest()
    binding: TokenBinding | None = None
    for candidate_hash, candidate in registry.items():
        if hmac.compare_digest(candidate_hash, digest):
            binding = candidate
    if binding is None:
        AUTH_FAILURES.labels(reason="invalid_token").inc()
        raise AuthenticationError("The provided token is not valid.")

    return Actor(actor_id=binding.actor_id, role=binding.role, authenticated=True)


def assert_high_risk_execution_allowed(actor: Actor, risk: object) -> None:
    """High-risk execution requires an ``sre`` or ``admin`` actor holding the rollback permission.

    This is the *authorization* half of the high-risk gate; the *approval* half lives in
    ``app/actions/approval.py``. Both must pass (threat T-08).
    """
    from app.domain.enums import RiskLevel  # local import keeps the enum module dependency-free

    if isinstance(risk, RiskLevel) and risk.at_least(RiskLevel.HIGH):
        actor.require(Permission.DEPLOYMENT_ROLLBACK)


__all__ = [
    "READ_PERMISSIONS",
    "ROLE_PERMISSIONS",
    "Actor",
    "Permission",
    "assert_high_risk_execution_allowed",
    "authenticate",
]
