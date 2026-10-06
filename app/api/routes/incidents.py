"""Incident, evidence and diagnosis endpoints (EPIC-03/04)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_actor, get_services
from app.application.container import Services
from app.core.security import Actor, Permission
from app.domain.enums import AuditEventType, IncidentStatus
from app.investigation.collector import EvidenceCollector
from app.investigation.investigator import Investigator

router = APIRouter(prefix="/incidents", tags=["incidents"])

ServicesDep = Annotated[Services, Depends(get_services)]
ActorDep = Annotated[Actor, Depends(get_actor)]


@router.get("")
async def list_incidents(
    services: ServicesDep,
    actor: ActorDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    actor.require(Permission.INCIDENTS_READ)
    if status_filter and status_filter.lower() in {"open", "active"}:
        incidents = await services.incidents.list_open(limit=limit)
    else:
        incidents = await services.incidents.list(
            status=IncidentStatus(status_filter.upper()) if status_filter else None, limit=limit
        )
    counts = await services.incidents.count_by_status()
    return {
        "incidents": [_serialise(item) for item in incidents],
        "counts": counts,
        "limit": limit,
    }


@router.get("/{incident_id}")
async def get_incident(incident_id: str, services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    actor.require(Permission.INCIDENTS_READ)
    incident = await services.incidents.get(incident_id)
    if incident is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("Incident not found.", details={"incident_id": incident_id})
    return {
        "incident": _serialise(incident),
        "evidence_count": await services.evidence.count_for_incident(incident_id),
        "run": (
            _serialise_run(await services.runs.get_for_incident(incident_id))
            if incident.agent_run_id or (await services.runs.get_for_incident(incident_id))
            else None
        ),
    }


@router.get("/{incident_id}/evidence")
async def list_evidence(
    incident_id: str,
    services: ServicesDep,
    actor: ActorDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    actor.require(Permission.EVIDENCE_READ)
    items = await services.evidence.list_for_incident(incident_id, limit=limit)
    return {
        "incident_id": incident_id,
        "evidence": [
            {
                "id": item.id,
                "source": item.source.value,
                "kind": item.kind.value,
                "summary": item.summary,
                "content": item.content,
                "timestamp": item.timestamp.isoformat(),
                "confidence": item.confidence.value,
                "reliability": item.reliability,
                "injections": item.injections,
                "simulated": item.simulated,
                "content_hash": item.content_hash[:16],
            }
            for item in items
        ],
        "count": len(items),
    }


@router.get("/{incident_id}/anomalies")
async def list_anomalies(
    incident_id: str, services: ServicesDep, actor: ActorDep
) -> dict[str, Any]:
    actor.require(Permission.INCIDENTS_READ)
    items = await services.anomalies.list_for_incident(incident_id)
    return {
        "incident_id": incident_id,
        "anomalies": [
            {
                "id": item.id,
                "metric": item.metric,
                "family": item.family.value,
                "baseline": item.baseline,
                "observed": item.observed,
                "z_score": item.z_score,
                "relative_deviation": item.relative_deviation,
                "severity": item.severity.value,
                "detected_at": item.detected_at.isoformat(),
                "deduplicated": item.deduplicated,
                "simulated": item.simulated,
            }
            for item in items
        ],
        "count": len(items),
    }


@router.post("/{incident_id}/evidence/collect")
async def collect_evidence(
    incident_id: str, services: ServicesDep, actor: ActorDep
) -> dict[str, Any]:
    """Re-run evidence collection for one incident, recording degradations honestly."""
    actor.require(Permission.EVIDENCE_COLLECT)
    from app.core.errors import NotFoundError

    incident = await services.incidents.get(incident_id)
    if incident is None:
        raise NotFoundError("Incident not found.", details={"incident_id": incident_id})

    container = services.container  # type: ignore[attr-defined]
    collector = EvidenceCollector(container.settings, container.integrations)
    report = await collector.collect(incident)
    persisted = await services.evidence.persist(incident_id, report.drafts)
    await services.incidents.update(
        incident_id, {"evidence_count": await services.evidence.count_for_incident(incident_id)}
    )
    await services.audit.append(
        AuditEventType.EVIDENCE_COLLECTED,
        actor=actor.actor_id,
        role=actor.role.value,
        incident_id=incident_id,
        outcome="collected",
        reason=f"{len(persisted)} items via API",
        payload={"sources": report.sources_queried, "degradations": report.degradations},
    )
    for degradation in report.degradations:
        await services.audit.append(
            AuditEventType.EVIDENCE_SOURCE_DEGRADED,
            actor=actor.actor_id,
            role=actor.role.value,
            incident_id=incident_id,
            outcome="degraded",
            payload=degradation,
        )
    return {
        "incident_id": incident_id,
        "collected": len(persisted),
        "evidence_ids": [item.id for item in persisted],
        "sources_queried": report.sources_queried,
        "degradations": report.degradations,
    }


@router.post("/{incident_id}/investigate", status_code=status.HTTP_200_OK)
async def investigate(incident_id: str, services: ServicesDep, actor: ActorDep) -> dict[str, Any]:
    """Investigate an incident *without* executing anything (read-only reasoning path)."""
    actor.require(Permission.AGENT_RUN)
    from app.core.errors import NotFoundError

    incident = await services.incidents.get(incident_id)
    if incident is None:
        raise NotFoundError("Incident not found.", details={"incident_id": incident_id})

    container = services.container  # type: ignore[attr-defined]
    evidence = await services.evidence.list_for_incident(incident_id)
    anomalies = await services.anomalies.list_for_incident(incident_id)
    anomaly = (
        {
            "metric": anomalies[-1].metric,
            "baseline": anomalies[-1].baseline,
            "observed": anomalies[-1].observed,
            "z_score": anomalies[-1].z_score,
            "severity": anomalies[-1].severity.value,
        }
        if anomalies
        else {}
    )
    investigator = Investigator(container.settings, reasoner=container.reasoner)
    diagnosis = await investigator.investigate(incident, evidence, anomaly=anomaly)
    await services.incidents.update(incident_id, {"diagnosis": diagnosis.model_dump(mode="json")})
    await services.audit.append(
        AuditEventType.INVESTIGATION_COMPLETED,
        actor=actor.actor_id,
        role=actor.role.value,
        incident_id=incident_id,
        outcome="completed",
        reason=diagnosis.hypothesis[:400],
        payload={"reasoner": diagnosis.reasoner, "confidence": diagnosis.confidence},
    )
    return {
        "incident_id": incident_id,
        "diagnosis": diagnosis.model_dump(mode="json"),
        "evidence_considered": len(evidence),
    }


def _serialise(incident: Any) -> dict[str, Any]:
    return {
        "id": incident.id,
        "type": incident.incident_type.value,
        "severity": incident.severity.value,
        "status": incident.status.value,
        "title": incident.title,
        "summary": incident.summary,
        "service": incident.service,
        "metric": incident.metric,
        "detected_at": incident.detected_at.isoformat(),
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        "agent_run_id": incident.agent_run_id,
        "verification_outcome": incident.verification_outcome,
        "escalation_reason": incident.escalation_reason,
        "jira_issue_key": incident.jira_issue_key,
        "slack_message_ts": incident.slack_message_ts,
        "evidence_count": incident.evidence_count,
        "diagnosis": incident.diagnosis,
        "response_target": {
            "acknowledge": incident.response_target[0],
            "resolve": incident.response_target[1],
        },
        "simulated": incident.simulated,
        "details": incident.details,
    }


def _serialise_run(run: Any | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "state": run.state.value,
        "version": run.version,
        "stage": run.stage,
        "reasoner": run.reasoner,
        "degraded_reason": run.degraded_reason,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


__all__ = ["router"]
