"""Audit trail endpoints (SEC-004).

Read-only by construction: there is no endpoint that edits or deletes an audit record, because
an audit trail an operator can rewrite is not an audit trail. ``/audit/verify`` recomputes the
hash chain and reports the first divergence — the check an investigator runs *before* trusting
anything else in the export.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_actor, get_services
from app.application.container import Services
from app.core.errors import ValidationFailed
from app.core.security import Actor, Permission
from app.domain.enums import AuditEventType

router = APIRouter(prefix="/audit", tags=["audit"])

ServicesDep = Annotated[Services, Depends(get_services)]
ActorDep = Annotated[Actor, Depends(get_actor)]


def serialise_row(row: Any) -> dict[str, Any]:
    """Audit rows are returned field-by-field: no ORM object crosses the API boundary."""
    return {
        "seq": row.seq,
        "occurred_at": row.occurred_at.isoformat(),
        "event_type": row.event_type,
        "actor": row.actor,
        "role": row.role,
        "incident_id": row.incident_id,
        "agent_run_id": row.agent_run_id,
        "tool_name": row.tool_name,
        "action_id": row.action_id,
        "approval_id": row.approval_id,
        "outcome": row.outcome,
        "reason": row.reason,
        "payload": row.payload,
        "prev_hash": row.prev_hash[:16],
        "entry_hash": row.entry_hash[:16],
    }


@router.get("/verify")
async def verify_chain(services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """Recompute the hash chain. The answer is the entry point of every investigation."""
    actor.require(Permission.AUDIT_READ)
    return await services.audit.verify_chain()


@router.get("/events")
async def list_events(
    services: ServicesDep,
    actor: ActorDep,
    incident_id: Annotated[str | None, Query(max_length=64)] = None,
    agent_run_id: Annotated[str | None, Query(max_length=64)] = None,
    event_type: Annotated[str | None, Query(max_length=48)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    actor.require(Permission.AUDIT_READ)
    parsed_type = None
    if event_type:
        try:
            parsed_type = AuditEventType(event_type)
        except ValueError as exc:
            raise ValidationFailed(
                f"Unknown audit event type '{event_type}'.", details={"event_type": event_type}
            ) from exc
    rows = await services.audit.list_events(
        incident_id=incident_id,
        agent_run_id=agent_run_id,
        event_type=parsed_type,
        limit=limit,
    )
    return {"events": [serialise_row(row) for row in rows], "count": len(rows)}


@router.get("/recent")
async def recent_events(
    services: ServicesDep,
    actor: ActorDep,
    hours: Annotated[int, Query(ge=1, le=168)] = 1,
) -> dict[str, Any]:
    actor.require(Permission.AUDIT_READ)
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = await services.audit.list_events(since=since, limit=500)
    return {"hours": hours, "count": len(rows), "events": [serialise_row(row) for row in rows]}


@router.get("/export")
async def export(
    services: ServicesDep,
    actor: ActorDep,
    incident_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> dict[str, Any]:
    """Export evidence for post-incident review, together with the chain verdict."""
    actor.require(Permission.AUDIT_READ)
    rows = await services.audit.list_events(incident_id=incident_id, limit=limit)
    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "incident_id": incident_id,
        "chain": await services.audit.verify_chain(),
        "events": [serialise_row(row) for row in rows],
    }


__all__ = ["router", "serialise_row"]
