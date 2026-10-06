"""Composition root (OPS-010).

Everything that has a lifetime or a dependency is built **here**, once, and injected. There is
no module-level singleton holding a database session, and no layer reaches for a global — which
is what makes the layers testable in isolation and the wiring auditable in one file.

Two entry points:

* :func:`build_container` — for the application, with a database engine and (optionally) a
  pre-built integration facade;
* :class:`Container.session` — a transaction scope that hands every service the repositories
  bound to the *same* session, so a workflow is atomic apart from its deliberate external calls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.executor import ActionExecutor
from app.agent.orchestrator import IncidentOrchestrator
from app.application.detection_service import DetectionService
from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.core.logging import get_logger
from app.core.security import Actor
from app.detection.detector import AnomalyDetector
from app.detection.thresholds import DetectionThresholds
from app.integrations.facade import IntegrationFacade, build_integrations
from app.investigation.collector import EvidenceCollector
from app.investigation.investigator import Investigator
from app.llm.provider import build_reasoner
from app.persistence.database import Database
from app.persistence.repositories import (
    ActionRepository,
    AgentRunRepository,
    AnomalyRepository,
    ApprovalRepository,
    AuditRepository,
    EvidenceRepository,
    IdempotencyStore,
    IncidentRepository,
    ToolInvocationRepository,
    VerificationRepository,
)
from app.policy.engine import PolicyEngine
from app.tools.registry import ToolRegistry, build_default_registry
from app.verification.engine import VerificationEngine

logger = get_logger(__name__)


@dataclass
class Services:
    """All repositories and use-case objects bound to one session."""

    session: AsyncSession
    incidents: IncidentRepository
    anomalies: AnomalyRepository
    evidence: EvidenceRepository
    runs: AgentRunRepository
    actions: ActionRepository
    approvals: ApprovalRepository
    verifications: VerificationRepository
    invocations: ToolInvocationRepository
    audit: AuditRepository
    idempotency: IdempotencyStore
    #: Back-reference so an API route can build a use case without a second dependency. The
    #: container holds no request-scoped state, so sharing it across requests is safe.
    container: Container


@dataclass
class Container:
    """Application-wide singletons plus a per-request service factory."""

    settings: Settings
    database: Database
    registry: ToolRegistry
    integrations: IntegrationFacade
    detector: AnomalyDetector
    policy: PolicyEngine
    reasoner: Any = None
    extras: dict[str, Any] = field(default_factory=dict)

    def services(self, session: AsyncSession) -> Services:
        return Services(
            session=session,
            container=self,
            incidents=IncidentRepository(session),
            anomalies=AnomalyRepository(session),
            evidence=EvidenceRepository(session),
            runs=AgentRunRepository(session),
            actions=ActionRepository(session),
            approvals=ApprovalRepository(session),
            verifications=VerificationRepository(session),
            invocations=ToolInvocationRepository(session),
            audit=AuditRepository(session),
            idempotency=IdempotencyStore(session),
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Services]:
        """Commit on success, roll back on failure, and never leak a half-written workflow."""
        async with self.database.session() as session:
            yield self.services(session)

    # -- use cases ---------------------------------------------------------- #

    def executor(self, services: Services) -> ActionExecutor:
        return ActionExecutor(
            self.settings,
            self.registry,
            self.policy,
            actions=services.actions,
            approvals=services.approvals,
            invocations=services.invocations,
            audit=services.audit,
            idempotency=services.idempotency,
            integrations=self.integrations,
        )

    def verifier(self, services: Services) -> VerificationEngine:
        return VerificationEngine(
            self.settings,
            self.integrations,
            verifications=services.verifications,
            actions=services.actions,
            audit=services.audit,
        )

    def orchestrator(self, services: Services) -> IncidentOrchestrator:
        return IncidentOrchestrator(
            self.settings,
            registry=self.registry,
            policy=self.policy,
            executor=self.executor(services),
            collector=EvidenceCollector(self.settings, self.integrations),
            investigator=Investigator(self.settings, reasoner=self.reasoner),
            verifier=self.verifier(services),
            incidents=services.incidents,
            anomalies=services.anomalies,
            evidence=services.evidence,
            runs=services.runs,
            actions=services.actions,
            approvals=services.approvals,
            verifications=services.verifications,
            audit=services.audit,
        )

    def detection(self, services: Services) -> DetectionService:
        service = DetectionService(
            self.settings,
            self.detector,
            services.incidents,
            services.anomalies,
            integrations=self.integrations,
            audit=services.audit,
        )
        service.orchestrator = self.orchestrator(services)  # type: ignore[attr-defined]
        return service

    async def health(self) -> dict[str, Any]:
        db_ok, db_error = await self.database.health()
        integrations_health = await self.integrations.health()
        degraded = sorted(
            name
            for name, report in integrations_health.items()
            if report.get("status") not in {"ok", "configured"}
        )
        return {
            "status": "ok" if db_ok and not degraded else "degraded",
            "database": {"status": "ok" if db_ok else "unavailable", "error": db_error},
            "integrations": integrations_health,
            "degraded_integrations": degraded,
            "llm": {
                "configured": self.settings.llm_configured,
                "reasoner": getattr(self.reasoner, "name", "deterministic"),
            },
            "simulated": self.settings.integrations_mode.value == "sandbox",
            "version": self.settings.version,
            "environment": self.settings.env.value,
        }

    async def aclose(self) -> None:
        await self.integrations.aclose()
        await self.database.engine.dispose()


def build_container(
    settings: Settings | None = None,
    *,
    integrations: IntegrationFacade | None = None,
    database: Database | None = None,
) -> Container:
    """Build the application graph. Raises :class:`ConfigurationError` on inconsistent config."""
    settings = settings or get_settings()

    try:
        registry = build_default_registry()
    except ValueError as exc:  # pragma: no cover - a registration bug is a startup failure
        raise ConfigurationError(f"Tool registry failed to build: {exc}") from exc

    thresholds = DetectionThresholds.from_settings(settings)
    detector = AnomalyDetector(thresholds)

    container = Container(
        settings=settings,
        database=database or Database(settings),
        registry=registry,
        integrations=integrations or build_integrations(settings),
        detector=detector,
        policy=PolicyEngine(registry, settings),
        reasoner=build_reasoner(settings),
    )
    logger.info(
        "container_built",
        tools=len(registry.names()),
        mode=container.integrations.mode.value,
        reasoner=getattr(container.reasoner, "name", "deterministic"),
        environment=settings.env.value,
    )
    return container


def system_actor(settings: Settings) -> Actor:
    """The agent acts as ``system`` — never as a human, and never as the caller.

    The system actor holds the SRE role, which is the minimum for high-risk actions, so policy
    evaluates the agent's proposals exactly as it would a human's. The identity being
    ``system`` (not a user id) is what keeps the audit trail honest about who acted.
    """
    from app.domain.enums import Role

    return Actor.system(role=Role.SRE)


__all__ = ["Container", "Services", "build_container", "system_actor"]
