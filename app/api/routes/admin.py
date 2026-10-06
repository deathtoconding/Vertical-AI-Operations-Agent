"""Admin, action, audit and release endpoints.

The autonomy switch is the most safety-relevant endpoint in the system: it changes what the
agent may do without a human, so it requires the admin permission and writes an audit event
naming the actor, the old level and the new one.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query

from app.api.dependencies import get_actor, get_services
from app.application.container import Services
from app.core.errors import NotFoundError
from app.core.security import Actor, Permission
from app.domain.enums import ActionStatus, AuditEventType
from app.domain.schemas import AutonomyRequest

router = APIRouter(tags=["admin"])

ServicesDep = Annotated[Services, Depends(get_services)]
ActorDep = Annotated[Actor, Depends(get_actor)]


@router.get("/admin/tools")
async def list_tools(services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """The complete inventory of what the agent *can* do — the security boundary, published."""
    actor.require(Permission.TOOLS_READ)
    registry = services.container.registry
    return {
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "permission": spec.permission,
                "risk": spec.risk.value,
                "requires_approval": spec.requires_approval,
                "timeout_seconds": spec.timeout_seconds,
                "max_retries": spec.max_retries,
                "idempotent": spec.idempotent,
                "simulated": spec.simulated,
                "input_schema": spec.input_schema,
            }
            for spec in registry.specs()
        ],
        "count": len(registry.names()),
        "note": "no shell, SQL, filesystem or arbitrary HTTP tool exists in this registry",
    }


@router.post("/admin/autonomy")
async def set_autonomy(
    services: ServicesDep, actor: ActorDep, payload: Annotated[AutonomyRequest, Body()]
) -> dict[str, Any]:
    actor.require(Permission.ADMIN_AUTONOMY)
    previous = services.container.settings.autonomy_level
    services.container.settings = services.container.settings.model_copy(
        update={"autonomy_level": payload.level}
    )
    services.container.policy.settings = services.container.settings
    await services.audit.append(
        AuditEventType.AUTONOMY_LEVEL_CHANGED,
        actor=actor.actor_id,
        role=actor.role.value,
        outcome="changed",
        reason=payload.reason[:300] or "autonomy level changed via admin API",
        payload={"previous": previous.value, "new": payload.level.value},
    )
    return {
        "previous": previous.value,
        "autonomy_level": payload.level.value,
        "note": (
            "observe_only denies every execution; approval_required is the default; "
            "selective_autonomy never makes high-risk actions automatic"
        ),
    }


@router.get("/admin/sandbox")
async def sandbox_state(services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    actor.require(Permission.INCIDENTS_READ)
    from app.sandbox.simulator import get_sandbox

    state = get_sandbox()
    return {
        "scenario": state.scenario,
        "active_release": state.active_release,
        "previous_release": state.previous_release,
        "healthy": not state.active_fault(),
        "rollback_count": state.rollback_count,
        "simulated": True,
    }


@router.post("/admin/sandbox/reset")
async def reset_sandbox(
    services: ServicesDep,
    actor: ActorDep,
    scenario: Annotated[str, Query(pattern="^(normal|A|B|C)$")] = "normal",
) -> dict[str, Any]:
    actor.require(Permission.INCIDENTS_WRITE)
    from app.sandbox.simulator import get_sandbox

    get_sandbox().reset(scenario=scenario)
    await services.audit.append(
        AuditEventType.VALIDATION_REJECTED,
        actor=actor.actor_id,
        role=actor.role.value,
        outcome="sandbox_reset",
        reason=f"sandbox scenario set to {scenario}",
    )
    return await sandbox_state(services, actor)


@router.post("/admin/pool/reset")
async def reset_pool(services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """Documented operator escape hatch (SRE runbook: database-failure)."""
    actor.require(Permission.ADMIN_RECOVERY)
    engine = services.container.database.engine
    await engine.dispose()
    return {"status": "disposed", "note": "the pool is rebuilt on the next connection"}


@router.get("/actions/{action_id}")
async def get_action(action_id: str, services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    actor.require(Permission.ACTIONS_READ)
    action = await services.actions.get(action_id)
    if action is None:
        raise NotFoundError("Action not found.", details={"action_id": action_id})
    approval = await services.approvals.get_for_action(action_id)
    return {
        "action": {
            "id": action.id,
            "incident_id": action.incident_id,
            "run_id": action.run_id,
            "tool_name": action.tool_name,
            "risk": action.risk.value,
            "permission": action.permission,
            "params": action.params,
            "rationale": action.rationale,
            "evidence_ids": action.evidence_ids,
            "requires_approval": action.requires_approval,
            "status": action.status.value,
            "policy_decision": action.policy_decision,
            "policy_reason": action.policy_reason,
            "result": action.result,
            "error": action.error,
            "payload_hash": action.canonical_hash[:32],
            "payload_intact": action.payload_is_intact,
            "simulated": action.simulated,
            "executed_at": action.executed_at.isoformat() if action.executed_at else None,
            "expected_state": action.expected_state,
        },
        "approval": (
            {
                "id": approval.id,
                "decision": approval.decision.value,
                "actor": approval.actor,
                "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
                "expires_at": approval.expires_at.isoformat(),
            }
            if approval
            else None
        ),
    }


@router.post("/actions/{action_id}/retry")
async def retry_action(action_id: str, services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """Retry a failed action. Idempotent by construction: the key comes from the payload."""
    actor.require(Permission.AGENT_RUN)
    action = await services.actions.get(action_id)
    if action is None:
        raise NotFoundError("Action not found.", details={"action_id": action_id})
    if action.status not in {
        ActionStatus.FAILED,
        ActionStatus.PROPOSED,
        ActionStatus.AWAITING_APPROVAL,
    }:
        from app.core.errors import ConflictError

        raise ConflictError(
            "Only failed or unexecuted actions can be retried.",
            details={"status": action.status.value},
        )
    executor = services.container.executor(services)
    outcome = await executor.execute(action, actor=actor)
    return {
        "action_id": action_id,
        "executed": outcome.executed,
        "replayed": outcome.replayed,
        "awaiting_approval": outcome.awaiting_approval,
        "reason": outcome.reason,
        "result": outcome.result.model_dump(mode="json") if outcome.result else None,
    }


@router.get("/audit/verify")
async def audit_verify(services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """Expose the hash-chain check as an endpoint an operator can call mid-incident."""
    actor.require(Permission.AUDIT_READ)
    return await services.audit.verify_chain()


@router.get("/release/current")
async def current_release(services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    actor.require(Permission.INCIDENTS_READ)
    state = await services.container.integrations.deployment_state()
    return {
        "active_release": state.get("active_release"),
        "previous_release": state.get("previous_release"),
        "deployed_at": state.get("deployed_at"),
        "healthy": state.get("healthy"),
        "available_releases": state.get("available_releases", []),
        "simulated": bool(state.get("simulated", False)),
        "version": services.container.settings.version,
    }


__all__ = ["router"]
