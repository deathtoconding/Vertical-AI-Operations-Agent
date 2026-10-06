"""Incident creation and deduplication (OPS-031).

Detection produces *facts about metrics*; this service produces *incidents*. The distinction is
where the two failure modes live:

* **Storms** — a sustained fault that re-fires every scan cycle. The dedup key is
  time-bucketed, so one fault is one incident, and recurrences are recorded on it.
* **Silent drops** — a second anomaly on an existing incident is still persisted as a
  detection event, because persistence or worsening is information an operator needs.

Neither path uses the LLM: whether an incident exists is a deterministic decision.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.telemetry import INCIDENTS_CREATED, INCIDENTS_DEDUPLICATED
from app.detection.incident_factory import (
    build_anomaly_values,
    build_incident,
    build_incident_from_detection,
)
from app.domain.enums import AuditEventType, Severity
from app.domain.incidents import DetectionResult, Incident
from app.persistence.models.base import new_prefixed_id
from app.persistence.repositories.incidents import AnomalyRepository, IncidentRepository

logger = get_logger(__name__)


class IncidentService:
    """Record detections and maintain the incident they belong to."""

    def __init__(
        self,
        settings: Settings,
        incidents: IncidentRepository,
        anomalies: AnomalyRepository,
        audit: Any,
    ) -> None:
        self.settings = settings
        self.incidents = incidents
        self.anomalies = anomalies
        self.audit = audit

    def dedup_key_for(self, detection: DetectionResult) -> str:
        """The key a new incident would receive — used to look for an existing one."""
        values = build_incident(
            detection, dedup_window_minutes=self.settings.detection_dedup_window_minutes
        )
        return str(values["dedup_key"])

    async def record_detection(
        self,
        detection: DetectionResult,
        *,
        actor: str,
        role: str = "system",
        simulated: bool = False,
    ) -> tuple[Incident | None, bool]:
        """Persist one detection event and return ``(incident, created)``.

        ``incident`` is the incident this detection belongs to (new or existing); ``created``
        says whether it was created here. The detection event is persisted either way.
        """
        existing = await self.incidents.get_by_dedup_key(self.dedup_key_for(detection))
        values, is_duplicate = build_incident_from_detection(
            detection,
            dedup_window_minutes=self.settings.detection_dedup_window_minutes,
            existing=existing,
            simulated=simulated,
        )

        if is_duplicate and existing is not None:
            incident_id = existing.id
            details = dict(existing.details or {})
            details["last_seen_at"] = values["detected_at"].isoformat()
            details["recurrences"] = int(details.get("recurrences", 0)) + 1
            await self.incidents.update(incident_id, {"details": details})
            INCIDENTS_DEDUPLICATED.labels(incident_type=existing.incident_type.value).inc()
            await self.audit.append(
                AuditEventType.ANOMALY_DEDUPLICATED,
                actor=actor,
                role=role,
                incident_id=incident_id,
                outcome="deduplicated",
                reason=f"{detection.metric} deviated again inside the dedup window",
                payload={"metric": detection.metric, "z_score": detection.z_score},
            )
            incident = existing
            created = False
        else:
            incident = await self.incidents.create({**values, "id": new_prefixed_id("INC")})
            created = True
            INCIDENTS_CREATED.labels(
                severity=incident.severity.value, incident_type=incident.incident_type.value
            ).inc()
            await self.audit.append(
                AuditEventType.INCIDENT_CREATED,
                actor=actor,
                role=role,
                incident_id=incident.id,
                outcome="created",
                reason=f"{detection.metric} deviation detected ({detection.severity.value})",
                payload={
                    "metric": detection.metric,
                    "baseline": detection.baseline,
                    "observed": detection.observed,
                    "z_score": detection.z_score,
                    "relative_deviation": detection.relative_deviation,
                    "severity": detection.severity.value,
                },
            )
            if incident.severity is Severity.SEV1:
                logger.error(
                    "sev1_incident_created",
                    incident_id=incident.id,
                    metric=detection.metric,
                    service=incident.service,
                )

        await self.anomalies.create(
            {
                "id": new_prefixed_id("ANM"),
                **build_anomaly_values(
                    detection,
                    incident_id=incident.id,
                    deduplicated=is_duplicate,
                    simulated=simulated,
                ),
            }
        )
        return incident, created


__all__ = ["IncidentService"]
