"""Detection and scenario endpoints (OPS-030/031, demo path).

``POST /detection/simulate`` is the documented way to inject a scenario (SRE runbooks call it),
and it is **honest about being a simulation**: the response carries ``simulated: true``, the
sandbox is reset, and the endpoint refuses to run at all when the app is configured for live
integrations. A simulation endpoint that silently worked against production would be a
foot-gun with a nice name.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends

from app.api.dependencies import get_actor, get_services
from app.application.container import Services
from app.application.detection_service import incident_type_for_metric
from app.core.config import IntegrationsMode
from app.core.errors import ValidationFailed
from app.core.security import Actor, Permission
from app.detection.detector import DEFAULT_METRICS
from app.domain.schemas import ScanRequest, SimulateRequest

router = APIRouter(tags=["detection"])

ServicesDep = Annotated[Services, Depends(get_services)]
ActorDep = Annotated[Actor, Depends(get_actor)]

SCENARIO_METRICS: dict[str, tuple[str, ...]] = {
    "A": ("error_rate", "latency_p95", "request_rate"),
    "B": ("latency_p95", "error_rate"),
    "C": ("payment_failure_rate", "error_rate"),
}


@router.post("/detection/scan")
async def scan(
    services: ServicesDep, actor: ActorDep, payload: Annotated[ScanRequest, Body()]
) -> dict[str, Any]:
    actor.require(Permission.INCIDENTS_WRITE)
    service = services.container.detection(services)
    metrics = tuple(payload.metrics) if payload.metrics else DEFAULT_METRICS
    for metric in metrics:
        if metric not in DEFAULT_METRICS and metric not in {
            "latency_p99",
            "saturation",
            "payment_failure_rate",
        }:
            raise ValidationFailed(f"Unknown metric '{metric}'.", details={"metric": metric})
    return await service.scan(
        service=payload.service,
        metrics=metrics,
        actor=actor,
        orchestrate=payload.orchestrate,
    )


@router.post("/detection/simulate")
async def simulate(
    services: ServicesDep, actor: ActorDep, payload: Annotated[SimulateRequest, Body()]
) -> dict[str, Any]:
    """Inject a scenario into the operated-system simulator, then detect it."""
    actor.require(Permission.INCIDENTS_WRITE)
    settings = services.container.settings
    if settings.integrations_mode is not IntegrationsMode.SANDBOX:
        raise ValidationFailed(
            "Scenario injection is only available in sandbox mode.",
            details={"integrations_mode": settings.integrations_mode.value},
        )

    from app.sandbox.simulator import get_sandbox

    sandbox = get_sandbox()
    if payload.reset:
        sandbox.reset(scenario=payload.scenario)

    service = services.container.detection(services)
    metrics = (
        DEFAULT_METRICS if payload.scenario == "normal" else SCENARIO_METRICS[payload.scenario]
    )
    result = await service.scan(actor=actor, metrics=metrics, orchestrate=payload.orchestrate)
    return {
        **result,
        "scenario": payload.scenario,
        "simulated": True,
        "note": (
            "sandbox scenario: every payload is labelled simulated and no real system was touched"
        ),
    }


@router.get("/detection/metrics")
async def known_metrics(services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    actor.require(Permission.METRICS_READ)
    return {
        "metrics": [
            {"metric": metric, "incident_type": incident_type_for_metric(metric).value}
            for metric in (*DEFAULT_METRICS, "latency_p99", "saturation", "payment_failure_rate")
        ]
    }


__all__ = ["SCENARIO_METRICS", "router"]
