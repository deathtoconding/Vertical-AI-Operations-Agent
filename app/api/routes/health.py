"""Health, readiness, metrics and SLO endpoints (SRE-001..005).

The three probes answer three different questions and must be able to disagree:

* ``/health``  — is the process alive? Always 200 while the event loop turns.
* ``/ready``   — can the agent actually do its job? 503 when the database is unavailable;
                 a *degraded* integration is reported but does not fail readiness, because the
                 agent can still investigate with fewer sources (and says so).
* ``/metrics`` — Prometheus exposition of every registered metric.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import get_actor, get_container, get_services
from app.application.container import Container, Services
from app.core.security import Actor, Permission
from app.core.telemetry import (
    ACTION_SUCCESS_RATE,
    AUDIT_CHAIN_BROKEN,
    SLO_METRIC_MAP,
    render_metrics,
)

router = APIRouter(tags=["observability"])

ContainerDep = Annotated[Container, Depends(get_container)]
ServicesDep = Annotated[Services, Depends(get_services)]
ActorDep = Annotated[Actor, Depends(get_actor)]


@router.get("/health")
async def health(container: ContainerDep) -> dict[str, Any]:
    """Liveness: the process is up. Deliberately does not check dependencies."""
    return {
        "status": "ok",
        "service": container.settings.service_name,
        "version": container.settings.version,
        "environment": container.settings.env.value,
        "simulated": container.settings.integrations_mode.value == "sandbox",
    }


@router.get("/ready")
async def ready(container: ContainerDep, response: Response) -> dict[str, Any]:
    """Readiness: dependencies the agent needs. 503 only when it cannot work at all."""
    report = await container.health()
    if report["database"]["status"] != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    # A degraded integration is reported, not fatal: partial evidence is better than none,
    # and the investigation layer records the degradation.
    return report


@router.get("/integrations/health")
async def integrations_health(container: ContainerDep, actor: ActorDep) -> dict[str, Any]:
    actor.require(Permission.INCIDENTS_READ)
    report = await container.integrations.health()
    degraded = [
        name for name, item in report.items() if item.get("status") not in {"ok", "configured"}
    ]
    return {
        "mode": container.integrations.mode.value,
        "integrations": report,
        "degraded": degraded,
        "healthy": not degraded,
    }


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")


@router.get("/slo")
async def slo(services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """The SLO/SLI surface, including the zero-tolerance security counters."""
    actor.require(Permission.INCIDENTS_READ)
    from prometheus_client import REGISTRY

    from app.core.telemetry import _normalise

    wanted = {
        "audit_integrity": AUDIT_CHAIN_BROKEN._name,
        "tool_execution_success": ACTION_SUCCESS_RATE._name,
    }
    values: dict[str, Any] = {}
    for sample in REGISTRY.collect():
        name = sample.name
        for key, metric_name in SLO_METRIC_MAP.items():
            if _normalise(name) == metric_name:
                values[key] = sum(
                    metric.value for metric in sample.samples if metric.name == sample.name
                )
        for key, metric_name in wanted.items():
            if _normalise(name) == metric_name:
                values[key] = sum(
                    metric.value for metric in sample.samples if metric.name == sample.name
                )

    counts = await services.incidents.count_by_status()
    verification = await services.verifications.outcome_counts(
        __import__("datetime").datetime.now(__import__("datetime").UTC)
        - __import__("datetime").timedelta(days=1)
    )
    return {
        "slis": values,
        "slo_targets": {
            "api_availability": ">= 99.9%",
            "detection_latency_p95": "< 60 s",
            "investigation_latency_p95": "< 5 min",
            "verification_latency_p95": "< 30 s",
            "tool_execution_success": ">= 99%",
            "unauthorized_high_risk_actions": "= 0",
            "audit_integrity": "= valid chain",
        },
        "incidents": counts,
        "verifications_last_24h": verification,
    }


__all__ = ["router"]
