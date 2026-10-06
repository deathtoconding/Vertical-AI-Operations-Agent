"""Detection service: statistics create incidents; the LLM is not involved (OPS-030/031).

The service is the only place where "a metric moved" becomes "an incident exists". It:

1. evaluates the configured metrics with the deterministic detector;
2. **always** persists the detection event, including duplicates — a worsening incident is
   information, and a deleted anomaly row is an audit hole;
3. deduplicates *incidents* by time-bucketed key so a sustained fault produces one incident
   rather than one per scan;
4. hands the new incident to the orchestrator.

Ordering matters: the incident row is committed before any LLM or external call, so a failure
downstream cannot lose the fact that a problem was detected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.persistence.repositories.incidents import AnomalyRepository, IncidentRepository

from app.application.incident_service import IncidentService
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import Actor
from app.core.telemetry import DETECTION_LATENCY
from app.detection.detector import DEFAULT_METRICS, AnomalyDetector
from app.domain.enums import AuditEventType, IncidentType

logger = get_logger(__name__)

#: Metrics that indicate money is being lost if they go unattended.
CRITICAL_METRICS = frozenset({"payment_failure_rate"})


class DetectionService:
    """Scan metric series and turn genuine deviations into incidents."""

    def __init__(
        self,
        settings: Settings,
        detector: AnomalyDetector,
        incidents: IncidentRepository,
        anomalies: AnomalyRepository,
        *,
        integrations: Any,
        audit: Any,
    ) -> None:
        self.settings = settings
        self.detector = detector
        self.incidents = incidents
        self.anomalies = anomalies
        self.integrations = integrations
        self.audit = audit
        self.incident_service = IncidentService(settings, incidents, anomalies, audit)
        self.thresholds = detector.thresholds
        self.service = settings.default_service
        self.metrics = DEFAULT_METRICS

    async def scan(
        self,
        *,
        service: str | None = None,
        metrics: tuple[str, ...] | None = None,
        actor: Actor | None = None,
        autonomy: Any = None,
        orchestrate: bool = True,
    ) -> dict[str, Any]:
        """Evaluate every configured metric; create/dedupe incidents; hand off to the agent."""
        service = service or self.service
        metrics = metrics or self.metrics
        actor = actor or Actor.system()

        evaluations: list[dict[str, Any]] = []
        created_ids: list[str] = []
        deduplicated_ids: list[str] = []

        for metric in metrics:
            detection = await self.detector.evaluate_metric(
                self.integrations.metrics, service, metric
            )
            evaluations.append(
                {
                    "metric": metric,
                    "outcome": detection.outcome,
                    "severity": detection.severity.value if detection.is_anomaly else None,
                    "baseline": detection.baseline,
                    "observed": detection.observed,
                    "z_score": detection.z_score,
                    "relative_deviation": detection.relative_deviation,
                    "sample_count": detection.sample_count,
                    "reason": detection.explanation,
                }
            )
            DETECTION_LATENCY.observe(detection.detail.get("duration_seconds", 0.0))

            if not detection.is_anomaly:
                continue

            incident, created = await self.incident_service.record_detection(
                detection,
                actor=actor.actor_id,
                role=actor.role.value,
                simulated=bool(self.settings.integrations_mode.value == "sandbox"),
            )
            if incident is None:  # pragma: no cover - the service always returns one
                continue
            if created:
                created_ids.append(incident.id)
            else:
                deduplicated_ids.append(incident.id)

        await self.audit.append(
            AuditEventType.ANOMALY_DETECTED,
            actor=actor.actor_id,
            role=actor.role.value,
            outcome="scanned",
            reason=f"evaluated {len(evaluations)} metrics",
            payload={
                "service": service,
                "created": created_ids,
                "deduplicated": deduplicated_ids,
                "evaluations": evaluations,
            },
        )

        results: list[dict[str, Any]] = []
        if orchestrate:
            for incident_id in created_ids:
                results.append(await self._orchestrate(incident_id, actor=actor, autonomy=autonomy))
        return {
            "service": service,
            "evaluations": evaluations,
            "incidents_created": created_ids,
            "incidents_deduplicated": deduplicated_ids,
            "runs": results,
            "simulated": self.settings.integrations_mode.value == "sandbox",
        }

    async def _orchestrate(
        self, incident_id: str, *, actor: Actor, autonomy: Any
    ) -> dict[str, Any]:
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return {"incident_id": incident_id, "run": None, "note": "no orchestrator wired"}
        snapshot = await orchestrator.handle_incident(incident_id, actor=actor, autonomy=autonomy)
        return {"incident_id": incident_id, **snapshot}


def incident_type_for_metric(metric: str) -> IncidentType:
    """Exposed for the API so a caller can predict the incident type of a scenario."""
    from app.detection.incident_factory import METRIC_TO_INCIDENT_TYPE
    from app.domain.enums import MetricFamily

    try:
        family = MetricFamily(metric)
    except ValueError:
        return IncidentType.API_ERROR_SPIKE
    return METRIC_TO_INCIDENT_TYPE.get(family, IncidentType.API_ERROR_SPIKE)


__all__ = ["CRITICAL_METRICS", "DetectionService", "incident_type_for_metric"]
