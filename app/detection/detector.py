"""The anomaly detector (OPS-030).

Contract:

* input: a metric series (from the metrics provider) and thresholds;
* output: a :class:`~app.domain.incidents.DetectionResult` with metric, baseline, observed,
  deviation, severity and ``detected_at``;
* properties: **deterministic** (same input, same output), **explainable** (every result
  carries the numbers and the reason), **quiet** (insufficient data yields
  ``insufficient_data`` rather than a false positive).

The detector never calls the LLM. "Does this look unusual?" is a statistics question, and
answering it with a language model would make an outage look like whatever the model felt like
that day.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.core.telemetry import ANOMALY_EVALUATIONS
from app.detection.baselines import (
    SeriesPoint,
    compute_baseline,
    peaked,
    relative_deviation,
    robust_z,
    split_window,
    summarise,
)
from app.detection.severity import family_from_metric, severity_from_signal
from app.detection.thresholds import (
    DOWNWARD_IS_BAD,
    IMPACT_FAMILIES,
    MIN_SAMPLES_OVERRIDE,
    DetectionThresholds,
)
from app.domain.enums import Severity
from app.domain.incidents import DetectionResult

logger = get_logger(__name__)

OUTCOME_NORMAL = "normal"
OUTCOME_ANOMALY = "anomaly"
OUTCOME_INSUFFICIENT = "insufficient_data"


def _parse_points(series: dict[str, Any], *, include_partial: bool = True) -> list[SeriesPoint]:
    points: list[SeriesPoint] = []
    for raw in series.get("points", []):
        if raw.get("partial") and not include_partial:
            continue
        try:
            timestamp = datetime.fromisoformat(str(raw["timestamp"]))
        except (KeyError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        try:
            value = float(raw["value"])
        except (KeyError, TypeError, ValueError):
            continue
        points.append(
            SeriesPoint(timestamp=timestamp, value=value, partial=bool(raw.get("partial")))
        )
    points.sort(key=lambda point: point.timestamp)
    return points


class AnomalyDetector:
    """Deterministic, statistical detection for error rate, latency, traffic and payments."""

    def __init__(self, thresholds: DetectionThresholds | None = None) -> None:
        self.thresholds = thresholds or DetectionThresholds()

    # ------------------------------------------------------------------ #

    def evaluate_series(
        self, series: dict[str, Any], *, metric: str | None = None, service: str | None = None
    ) -> DetectionResult:
        """Evaluate one metric series and return a fully-populated detection result."""
        thresholds = self.thresholds
        resolved_metric = metric or str(series.get("metric", "unknown"))
        resolved_service = service or str(series.get("service", "unknown"))
        family = family_from_metric(resolved_metric)

        points = _parse_points(series, include_partial=thresholds.include_partial_point)
        minimum = MIN_SAMPLES_OVERRIDE.get(resolved_metric, thresholds.min_samples)
        now = datetime.now(UTC)

        window_start = points[0].timestamp if points else now
        window_end = points[-1].timestamp if points else now

        if len(points) < minimum:
            ANOMALY_EVALUATIONS.labels(metric=resolved_metric, outcome=OUTCOME_INSUFFICIENT).inc()
            return DetectionResult(
                metric=resolved_metric,
                family=family,
                service=resolved_service,
                baseline=points[-1].value if points else 0.0,
                observed=points[-1].value if points else 0.0,
                deviation=0.0,
                z_score=0.0,
                relative_deviation=0.0,
                sample_count=len(points),
                severity=Severity.SEV4,
                is_anomaly=False,
                outcome=OUTCOME_INSUFFICIENT,
                window_start=window_start,
                window_end=window_end,
                explanation=(
                    f"only {len(points)} samples available, {minimum} required: "
                    "not enough history to judge (this is not a 'healthy' verdict)"
                ),
                detail={"thresholds": thresholds.describe(), "required_samples": minimum},
            )

        baseline_points, recent_points = split_window(
            points,
            recent_fraction=thresholds.recent_fraction,
            min_recent=thresholds.min_recent,
        )
        if not recent_points:
            baseline_points, recent_points = points[:minimum], points[minimum:]

        baseline = compute_baseline(baseline_points)
        observed = summarise(recent_points)
        peak = peaked(recent_points)
        z = robust_z(observed, baseline)
        relative = relative_deviation(observed, baseline)
        direction = "up" if relative >= 0 else "down"

        anomalous_direction = (
            direction == "down" if resolved_metric in DOWNWARD_IS_BAD else direction == "up"
        )
        strong_enough = abs(z) >= thresholds.z_threshold and abs(relative) >= (
            thresholds.min_relative_deviation
            if not (resolved_metric in IMPACT_FAMILIES and observed >= baseline.median)
            else min(thresholds.min_relative_deviation, 0.25)
        )
        is_anomaly = bool(strong_enough and anomalous_direction)

        severity, explanation = (
            severity_from_signal(family, observed=observed, relative_deviation=relative, z_score=z)
            if is_anomaly
            else (Severity.SEV4, "within the expected range for this series")
        )

        outcome = OUTCOME_ANOMALY if is_anomaly else OUTCOME_NORMAL
        ANOMALY_EVALUATIONS.labels(metric=resolved_metric, outcome=outcome).inc()

        detail = {
            "thresholds": thresholds.describe(),
            "baseline_samples": baseline.sample_count,
            "observed_samples": len(recent_points),
            "baseline_mad": baseline.mad,
            "baseline_sigma": round(baseline.sigma, 8),
            "peak_observed": peak,
            "direction": direction,
            "partial_point_included": any(point.partial for point in recent_points),
        }

        logger.info(
            "anomaly_evaluation",
            metric=resolved_metric,
            outcome=outcome,
            baseline=round(baseline.median, 6),
            observed=round(observed, 6),
            z=round(z, 3) if z != float("inf") else "inf",
            relative=round(relative, 3),
            severity=severity.value,
        )

        return DetectionResult(
            metric=resolved_metric,
            family=family,
            service=resolved_service,
            baseline=baseline.median,
            observed=observed,
            deviation=observed - baseline.median,
            z_score=z,
            relative_deviation=relative,
            sample_count=len(points),
            severity=severity,
            detected_at=now,
            is_anomaly=is_anomaly,
            outcome=outcome,
            window_start=baseline_points[0].timestamp,
            window_end=recent_points[-1].timestamp,
            explanation=explanation,
            detail=detail,
        )

    async def evaluate_metric(
        self,
        provider: Any,
        service: str,
        metric: str,
        *,
        window_minutes: int | None = None,
        thresholds: DetectionThresholds | None = None,
    ) -> DetectionResult:
        """Fetch a series from a provider and evaluate it."""
        active = thresholds or self.thresholds
        series = await provider.query_metric(
            service, metric, window_minutes or active.window_minutes
        )
        return self.evaluate_series(series, metric=metric, service=service)

    async def scan(
        self,
        provider: Any,
        service: str,
        metrics: list[str],
        *,
        thresholds: DetectionThresholds | None = None,
    ) -> list[DetectionResult]:
        """Evaluate several metrics; a failed metric is reported, never silently skipped."""
        results: list[DetectionResult] = []
        for metric in metrics:
            try:
                results.append(
                    await self.evaluate_metric(provider, service, metric, thresholds=thresholds)
                )
            except Exception as exc:
                logger.warning("metric_evaluation_failed", metric=metric, error=type(exc).__name__)
                results.append(
                    DetectionResult(
                        metric=metric,
                        family=family_from_metric(metric),
                        service=service,
                        baseline=0.0,
                        observed=0.0,
                        deviation=0.0,
                        z_score=0.0,
                        relative_deviation=0.0,
                        sample_count=0,
                        severity=Severity.SEV4,
                        is_anomaly=False,
                        outcome=OUTCOME_INSUFFICIENT,
                        window_start=datetime.now(UTC),
                        window_end=datetime.now(UTC),
                        explanation=f"metric could not be evaluated: {type(exc).__name__}",
                    )
                )
        return results


DEFAULT_METRICS: tuple[str, ...] = ("error_rate", "latency_p95", "request_rate")


__all__ = [
    "DEFAULT_METRICS",
    "OUTCOME_ANOMALY",
    "OUTCOME_INSUFFICIENT",
    "OUTCOME_NORMAL",
    "AnomalyDetector",
]
