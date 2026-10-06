"""Approval endpoints (OPS-061).

An approval is bound to one payload hash. The API surface reflects that:

* the *server* decides what the payload hash is — a client cannot propose one;
* a decision must carry the hash it believes it is approving, so a stale UI tab cannot approve
  a payload that changed underneath it;
* a second decision on the same approval is a 409, not a silent overwrite.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query

from app.api.dependencies import get_actor, get_services
from app.application.container import Services
from app.core.errors import NotFoundError, ValidationFailed
from app.core.security import Actor, Permission
from app.core.telemetry import APPROVAL_LATENCY, APPROVALS_DECIDED
from app.domain.enums import ApprovalDecision, AuditEventType
from app.domain.schemas import ApprovalDecisionRequest
from app.persistence.models.base import utcnow

router = APIRouter(prefix="/approvals", tags=["approvals"])

ServicesDep = Annotated[Services, Depends(get_services)]
ActorDep = Annotated[Actor, Depends(get_actor)]


@router.get("")
async def list_approvals(
    services: ServicesDep,
    actor: ActorDep,
    pending_only: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    actor.require(Permission.INCIDENTS_READ)
    approvals = await services.approvals.list_pending(limit=limit) if pending_only else []
    return {
        "approvals": [
            {
                "id": item.id,
                "action_id": item.action_id,
                "incident_id": item.incident_id,
                "decision": item.decision.value,
                "payload_hash": item.payload_hash[:32],
                "requested_at": item.requested_at.isoformat(),
                "expires_at": item.expires_at.isoformat(),
                "expired": item.is_expired,
                "risk": (
                    (await services.actions.get(item.action_id)).risk.value
                    if await services.actions.get(item.action_id)
                    else None
                ),
                "tool_name": (
                    (await services.actions.get(item.action_id)).tool_name
                    if await services.actions.get(item.action_id)
                    else None
                ),
            }
            for item in approvals
        ],
        "count": len(approvals),
    }


@router.get("/{approval_id}")
async def get_approval(approval_id: str, services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    actor.require(Permission.INCIDENTS_READ)
    approval = await services.approvals.get(approval_id)
    if approval is None:
        raise NotFoundError("Approval not found.", details={"approval_id": approval_id})
    action = await services.actions.get(approval.action_id)
    return {
        "approval": {
            "id": approval.id,
            "action_id": approval.action_id,
            "incident_id": approval.incident_id,
            "decision": approval.decision.value,
            "actor": approval.actor,
            "role": approval.role,
            "reason": approval.reason,
            "payload_hash": approval.payload_hash[:32],
            "requested_at": approval.requested_at.isoformat(),
            "expires_at": approval.expires_at.isoformat(),
            "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
            "expired": approval.is_expired,
        },
        "action": (
            {
                "id": action.id,
                "tool_name": action.tool_name,
                "risk": action.risk.value,
                "params": action.params,
                "rationale": action.rationale,
                "status": action.status.value,
                "payload_hash": action.canonical_hash[:32],
                "policy_decision": action.policy_decision,
                "policy_reason": action.policy_reason,
            }
            if action
            else None
        ),
    }


@router.post("/{approval_id}/decision")
async def decide(
    approval_id: str,
    services: ServicesDep,
    actor: ActorDep,
    payload: Annotated[ApprovalDecisionRequest, Body()],
) -> dict[str, Any]:
    actor.require(Permission.APPROVALS_DECIDE)
    approval = await services.approvals.get(approval_id)
    if approval is None:
        raise NotFoundError("Approval not found.", details={"approval_id": approval_id})
    if approval.decision is not ApprovalDecision.PENDING:
        from app.core.errors import ConflictError

        raise ConflictError(
            "This approval has already been decided.", details={"decision": approval.decision.value}
        )
    if approval.is_expired:
        raise ValidationFailed("This approval request has expired; re-plan the incident.")
    if payload.decision is ApprovalDecision.PENDING:
        raise ValidationFailed("A decision must be APPROVED or REJECTED.")
    if (
        not approval.payload_hash.startswith(payload.payload_hash)
        and approval.payload_hash != payload.payload_hash
    ):
        raise ValidationFailed(
            "The payload hash does not match the approval request; refresh before deciding.",
            details={"expected_prefix": approval.payload_hash[:16]},
        )

    decided = await services.approvals.decide(
        approval_id,
        decision=payload.decision,
        actor=actor.actor_id,
        role=actor.role.value,
        reason=payload.reason,
        expected_payload_hash=approval.payload_hash,
    )
    if decided is None:  # pragma: no cover - guarded above
        raise NotFoundError("Approval not found.", details={"approval_id": approval_id})

    APPROVALS_DECIDED.labels(decision=decided.decision.value.lower()).inc()
    decided_at = decided.decided_at or utcnow()
    APPROVAL_LATENCY.observe(max(0.0, (decided_at - decided.requested_at).total_seconds()))
    await services.audit.append(
        (
            AuditEventType.APPROVAL_GRANTED
            if decided.decision is ApprovalDecision.APPROVED
            else AuditEventType.APPROVAL_REJECTED
        ),
        actor=actor.actor_id,
        role=actor.role.value,
        incident_id=decided.incident_id,
        agent_run_id=decided.run_id,
        action_id=decided.action_id,
        approval_id=decided.id,
        outcome=decided.decision.value.lower(),
        reason=payload.reason[:300],
        payload={"payload_hash": decided.payload_hash[:32]},
    )
    return {
        "approval": {
            "id": decided.id,
            "decision": decided.decision.value,
            "actor": decided.actor,
            "role": decided.role,
            "decided_at": decided.decided_at.isoformat() if decided.decided_at else None,
        },
        "next": "resume the run to execute and verify"
        if decided.decision is ApprovalDecision.APPROVED
        else "run will not execute this action",
    }


__all__ = ["router"]
