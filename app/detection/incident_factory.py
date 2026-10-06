"""Turn detection results into incidents, with deduplication (OPS-031).

Two failure modes are being prevented here, and both are worse than they sound:

* **Incident storms** — one problem producing one incident per detection cycle pages a human
  dozens of times. Deduplication is therefore time-bucketed, not per-event.
* **Silent drops** — a second anomaly on an existing incident must still be *recorded*
  (it is evidence of persistence or worsening), even though it does not create a new incident.

The incident type is derived from the metric family, so a latency spike never becomes an
"error spike" incident with the wrong playbook attached.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.enums import IncidentType, MetricFamily, Severity
from app.domain.incidents import DetectionResult

METRIC_TO_INCIDENT_TYPE: dict[MetricFamily, IncidentType] = {
    MetricFamily.ERROR_RATE: IncidentType.API_ERROR_SPIKE,
    MetricFamily.LATENCY_P95: IncidentType.API_LATENCY_SPIKE,
    MetricFamily.LATENCY_P99: IncidentType.API_LATENCY_SPIKE,
    MetricFamily.REQUEST_RATE: IncidentType.TRAFFIC_ANOMALY,
    MetricFamily.SATURATION: IncidentType.API_LATENCY_SPIKE,
    MetricFamily.PAYMENT_FAILURE_RATE: IncidentType.SUBSCRIPTION_PAYMENT_ANOMALY,
}

TITLE_TEMPLATES: dict[IncidentType, str] = {
    IncidentType.API_ERROR_SPIKE: "{service}: API error rate spike ({observed})",
    IncidentType.API_LATENCY_SPIKE: "{service}: latency degradation on {metric} ({observed})",
    IncidentType.TRAFFIC_ANOMALY: "{service}: traffic anomaly on {metric} ({observed})",
    IncidentType.SUBSCRIPTION_PAYMENT_ANOMALY: (
        "{service}: subscription payment failures elevated ({observed})"
    ),
    IncidentType.DEPLOYMENT_REGRESSION: "{service}: deployment regression detected",
    IncidentType.TOOL_FAILURE: "{service}: repeated automation tool failures",
}


def dedup_window_seconds(minutes: int) -> int:
    return max(60, minutes * 60)


def build_dedup_key(
    detection: DetectionResult, *, window_minutes: int, now: datetime | None = None
) -> str:
    """Bucket key: same service + metric + direction + time bucket ⇒ same incident."""
    moment = now or datetime.now(UTC)
    bucket = int(moment.timestamp()) // dedup_window_seconds(window_minutes)
    direction = "up" if detection.relative_deviation >= 0 else "down"
    raw = f"{detection.service}|{detection.metric}|{direction}|{bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _format_observed(detection: DetectionResult) -> str:
    if detection.family is MetricFamily.ERROR_RATE or (
        detection.family is MetricFamily.PAYMENT_FAILURE_RATE
    ):
        return f"{detection.observed * 100:.1f}% vs baseline {detection.baseline * 100:.1f}%"
    if detection.family in {MetricFamily.LATENCY_P95, MetricFamily.LATENCY_P99}:
        return f"{detection.observed:.0f}ms vs baseline {detection.baseline:.0f}ms"
    if detection.family is MetricFamily.SATURATION:
        return f"{detection.observed * 100:.0f}% saturation"
    return f"{detection.observed:.2f} vs baseline {detection.baseline:.2f}"


def build_incident(
    detection: DetectionResult,
    *,
    dedup_window_minutes: int,
    now: datetime | None = None,
    simulated: bool = False,
) -> dict[str, Any]:
    """Build the row values for a new incident. Persistence is the repository's job."""
    moment = now or datetime.now(UTC)
    incident_type = METRIC_TO_INCIDENT_TYPE.get(detection.family, IncidentType.API_ERROR_SPIKE)
    title = TITLE_TEMPLATES[incident_type].format(
        service=detection.service, metric=detection.metric, observed=_format_observed(detection)
    )
    summary = (
        f"{detection.metric} on {detection.service} deviated from its baseline: "
        f"{detection.explanation}. Baseline {detection.baseline:.6g}, observed "
        f"{detection.observed:.6g} ({detection.relative_deviation * 100:+.0f}%), "
        f"robust z={detection.z_score:.1f}."
    )
    return {
        "incident_type": incident_type.value,
        "severity": detection.severity.value,
        "status": "OPEN",
        "title": title[:300],
        "summary": summary,
        "service": detection.service,
        "metric": detection.metric,
        "dedup_key": build_dedup_key(detection, window_minutes=dedup_window_minutes, now=moment),
        "detected_at": detection.detected_at,
        "simulated": simulated,
        "details": {
            "detection": {
                "metric": detection.metric,
                "family": detection.family.value,
                "baseline": detection.baseline,
                "observed": detection.observed,
                "deviation": detection.deviation,
                "z_score": detection.z_score if detection.z_score != float("inf") else None,
                "relative_deviation": detection.relative_deviation,
                "sample_count": detection.sample_count,
                "window_start": detection.window_start.isoformat(),
                "window_end": detection.window_end.isoformat(),
                "outcome": detection.outcome,
                "explanation": detection.explanation,
                "thresholds": detection.detail.get("thresholds", {}),
            }
        },
    }


def build_incident_from_detection(
    detection: DetectionResult,
    *,
    dedup_window_minutes: int,
    existing: Any | None = None,
    now: datetime | None = None,
    simulated: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Return ``(incident_values, is_duplicate)``.

    When ``existing`` is provided the anomaly is attributed to it, and the caller must still
    persist the detection event — a worsening incident is information.
    """
    values = build_incident(
        detection, dedup_window_minutes=dedup_window_minutes, now=now, simulated=simulated
    )
    return values, existing is not None


def build_anomaly_values(
    detection: DetectionResult, *, incident_id: str | None, deduplicated: bool, simulated: bool
) -> dict[str, Any]:
    """Row values for the detection event itself (always persisted)."""
    return {
        "incident_id": incident_id,
        "service": detection.service,
        "metric": detection.metric,
        "family": detection.family.value,
        "baseline": detection.baseline,
        "observed": detection.observed,
        "deviation": detection.deviation,
        "z_score": detection.z_score if detection.z_score != float("inf") else 9999.0,
        "relative_deviation": detection.relative_deviation,
        "sample_count": detection.sample_count,
        "severity": detection.severity.value,
        "window_start": detection.window_start,
        "window_end": detection.window_end,
        "detected_at": detection.detected_at,
        "dedup_key": f"{detection.service}:{detection.metric}",
        "deduplicated": deduplicated,
        "simulated": simulated,
        "details": {
            "outcome": detection.outcome,
            "explanation": detection.explanation,
            **detection.detail,
        },
    }


def dedup_cutoff(window_minutes: int, now: datetime | None = None) -> datetime:
    moment = now or datetime.now(UTC)
    return moment - timedelta(seconds=dedup_window_seconds(window_minutes))


def severity_rank(severity: Severity) -> int:
    return severity.rank


__all__ = [
    "METRIC_TO_INCIDENT_TYPE",
    "TITLE_TEMPLATES",
    "build_anomaly_values",
    "build_dedup_key",
    "build_incident",
    "build_incident_from_detection",
    "dedup_cutoff",
    "dedup_window_seconds",
    "severity_rank",
]
