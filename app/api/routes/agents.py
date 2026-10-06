"""Agent run endpoints (OPS-002/050/060).

``POST /agents/runs`` starts a workflow for an existing incident. The response always states
``state``, ``requires_human`` and (when applicable) ``pending_approval_id`` — the API never
leaves a caller guessing whether the agent is working, waiting or finished.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, status

from app.api.dependencies import get_actor, get_services
from app.application.container import Services
from app.core.errors import NotFoundError
from app.core.security import Actor, Permission
from app.domain.schemas import ReasonRequest, StartRunRequest

router = APIRouter(prefix="/agents", tags=["agents"])

ServicesDep = Annotated[Services, Depends(get_services)]
ActorDep = Annotated[Actor, Depends(get_actor)]


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    services: ServicesDep, actor: ActorDep, payload: Annotated[StartRunRequest, Body()]
) -> dict[str, Any]:
    actor.require(Permission.AGENT_RUN)
    incident = await services.incidents.get(payload.incident_id)
    if incident is None:
        raise NotFoundError("Incident not found.", details={"incident_id": payload.incident_id})

    orchestrator = services.container.orchestrator(services)
    return await orchestrator.handle_incident(incident.id, actor=actor, autonomy=payload.autonomy)


@router.get("/runs")
async def list_runs(
    services: ServicesDep,
    actor: ActorDep,
    state: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    actor.require(Permission.AGENT_RUNS_READ)
    from app.domain.enums import AgentState

    runs = await services.runs.list_runs(
        states=[AgentState(state.upper())] if state else None, limit=limit
    )
    return {
        "runs": [
            {
                "id": run.id,
                "incident_id": run.incident_id,
                "state": run.state.value,
                "stage": run.stage,
                "version": run.version,
                "reasoner": run.reasoner,
                "degraded_reason": run.degraded_reason,
                "failure_reason": run.failure_reason,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }
            for run in runs
        ],
        "count": len(runs),
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str, services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    actor.require(Permission.AGENT_RUNS_READ)
    run = await services.runs.get(run_id)
    if run is None:
        raise NotFoundError("Agent run not found.", details={"run_id": run_id})
    transitions = await services.runs.list_transitions(run_id)
    invocations = await services.invocations.list_for_run(run_id)
    return {
        "run": {
            "id": run.id,
            "incident_id": run.incident_id,
            "state": run.state.value,
            "stage": run.stage,
            "version": run.version,
            "reasoner": run.reasoner,
            "degraded_reason": run.degraded_reason,
            "failure_reason": run.failure_reason,
            "diagnosis": run.diagnosis,
            "plan": run.plan,
            "notes": run.notes,
            "simulated": run.simulated,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        },
        "transitions": [
            {
                "from_state": item.from_state.value,
                "to_state": item.to_state.value,
                "actor": item.actor,
                "reason": item.reason,
                "version": item.version,
                "occurred_at": item.occurred_at.isoformat(),
            }
            for item in transitions
        ],
        "tool_invocations": [
            {
                "id": item.id,
                "tool_name": item.tool_name,
                "risk": item.risk.value,
                "outcome": item.outcome.value,
                "duration_ms": item.duration_ms,
                "error": item.error,
                "occurred_at": item.occurred_at.isoformat(),
            }
            for item in invocations
        ],
    }


@router.post("/runs/{run_id}/investigate")
async def investigate_run(run_id: str, services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """Continue a run that was interrupted before planning, or re-investigate on demand."""
    actor.require(Permission.AGENT_RUN)
    run = await services.runs.get(run_id)
    if run is None:
        raise NotFoundError("Agent run not found.", details={"run_id": run_id})
    orchestrator = services.container.orchestrator(services)
    return await orchestrator.handle_incident(run.incident_id, actor=actor)


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """Resume a run that is waiting for approval, or hand an escalated run back."""
    actor.require(Permission.AGENT_RUN)
    orchestrator = services.container.orchestrator(services)
    return await orchestrator.resume(run_id, actor=actor)


@router.post("/runs/{run_id}/reverify")
async def reverify_run(
    run_id: str,
    services: ServicesDep,
    actor: ActorDep,
    payload: Annotated[ReasonRequest | None, Body()] = None,
) -> dict[str, Any]:
    """Re-run verification (SRE runbook: distrust a verification result without editing data)."""
    actor.require(Permission.AGENT_RUN)
    run = await services.runs.get(run_id)
    if run is None:
        raise NotFoundError("Agent run not found.", details={"run_id": run_id})

    verifier = services.container.verifier(services)
    actions = await services.actions.list_for_incident(run.incident_id)
    executed = [item for item in actions if item.result]
    result = (
        await verifier.verify_action(executed[-1].id, actor=actor.actor_id)
        if executed
        else await verifier.verify_incident(run.incident_id, actor=actor.actor_id)
    )
    await services.audit.append(
        __import__(
            "app.domain.enums", fromlist=["AuditEventType"]
        ).AuditEventType.VERIFICATION_COMPLETED,
        actor=actor.actor_id,
        role=actor.role.value,
        incident_id=run.incident_id,
        agent_run_id=run.id,
        outcome=result.outcome.value,
        reason=f"re-verification requested: {result.reason}"[:500],
    )
    return {
        "run_id": run_id,
        "verification_id": result.id,
        "outcome": result.outcome.value,
        "reason": result.reason,
        "checks": result.checks,
    }


@router.post("/recovery")
async def recover_runs(services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """Recover interrupted runs (documented, idempotent, and policy-re-checked)."""
    actor.require(Permission.ADMIN_RECOVERY)
    from app.agent.recovery import recover_interrupted_runs

    return await recover_interrupted_runs(services.container, services, actor=actor)


__all__ = ["router"]
