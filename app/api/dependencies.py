"""FastAPI dependencies: authentication, authorization, and request-scoped services.

The API layer owns HTTP concerns only. It authenticates, authorizes, validates and translates;
business rules live behind it. Two rules are enforced here and nowhere else:

* **Every request is authenticated** except health/readiness/metrics. There is no "internal"
  bypass path, so a reachable port is not an open door.
* **Authorization denials are audited.** A 403 that leaves no trace is indistinguishable from
  a bug, and an operator investigating "why did the agent do that?" needs the trace.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

from fastapi import Depends, Header, Request

from app.application.container import Container, Services
from app.core.config import Settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.security import Actor, Permission, authenticate
from app.domain.enums import AuditEventType


def get_container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - lifespan always sets this
        raise RuntimeError("application container is not initialised")
    return cast(Container, container)


def get_settings_from_app(request: Request) -> Settings:
    return get_container(request).settings


async def get_actor(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Actor:
    settings = get_settings_from_app(request)
    try:
        return authenticate(authorization, settings)
    except AuthenticationError as exc:
        container = getattr(request.app.state, "container", None)
        if container is not None:
            # Record the failed attempt outside the request's data transaction; a failed login
            # must not be able to roll back legitimate work.
            async with container.transaction() as services:
                await services.audit.append(
                    AuditEventType.AUTHENTICATION_FAILED,
                    actor="anonymous",
                    role="anonymous",
                    outcome="denied",
                    reason=str(exc)[:300],
                    payload={"path": request.url.path},
                )
        raise


def require(*permissions: Permission) -> Any:
    """Dependency factory: authenticate, then require every listed permission."""

    async def _guard(actor: Annotated[Actor, Depends(get_actor)]) -> Actor:
        for permission in permissions:
            actor.require(permission)
        return actor

    return _guard


async def get_services(request: Request) -> AsyncIterator[Services]:
    """A committed transaction per request, so a failure mid-request writes nothing."""
    container = get_container(request)
    async with container.transaction() as services:
        yield services


def get_container_services(
    request: Request,
) -> tuple[Container, Any]:
    return get_container(request), get_services


async def audit_authorization_denial(request: Request, actor: Actor, error: Exception) -> None:
    container = getattr(request.app.state, "container", None)
    if container is None:
        return
    async with container.transaction() as services:
        await services.audit.append(
            AuditEventType.AUTHORIZATION_DENIED,
            actor=actor.actor_id,
            role=actor.role.value,
            outcome="denied",
            reason=str(error)[:300],
            payload={"path": request.url.path},
        )


async def audit_validation_rejection(request: Request, error: Exception) -> None:
    """Record a rejected payload, best-effort, without echoing what was rejected.

    Counted *and* audited (SEC-006): a burst of 422s is how a broken client, a fuzzer or a
    probing request looks from here, and neither should be invisible. The payload is never
    stored — it failed validation, so it may contain anything, including credentials.
    """
    container = getattr(request.app.state, "container", None)
    if container is None:
        return
    try:
        actor = authenticate(request.headers.get("Authorization"), container.settings)
    except Exception:
        actor = Actor.system()
    async with container.transaction() as services:
        await services.audit.append(
            AuditEventType.VALIDATION_REJECTED,
            actor=actor.actor_id,
            role=actor.role.value,
            outcome="rejected",
            reason=str(error)[:300],
            payload={"path": request.url.path, "method": request.method},
        )


__all__ = [
    "AuthorizationError",
    "audit_validation_rejection",
    "get_actor",
    "get_container",
    "get_services",
    "get_settings_from_app",
    "require",
]
