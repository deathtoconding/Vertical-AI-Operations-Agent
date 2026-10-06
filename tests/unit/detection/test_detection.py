"""Detection unit tests (OPS-030/031).

The property under test is *determinism*: the same series must always produce the same
verdict, with no model in the loop. A detector that can be talked into (or out of) an incident
is not a detector.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.detection.baselines import (
    SeriesPoint,
    compute_baseline,
    robust_z,
    split_window,
    summarise,
)
from app.detection.detector import AnomalyDetector
from app.detection.incident_factory import build_dedup_key, build_incident
from app.detection.severity import family_from_metric, severity_from_signal
from app.detection.thresholds import DetectionThresholds
from app.domain.enums import IncidentType, MetricFamily, Severity

pytestmark = [pytest.mark.story("OPS-030"), pytest.mark.unit]

NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)


class FakeMetrics:
    """A metrics provider whose series is fixed by the test — no sandbox, no clock."""

    def __init__(self, service: str, metric: str, values: list[float]) -> None:
        self.service = service
        self.metric = metric
        self.values = values

    async def query_metric(self, service: str, metric: str, window_minutes: int) -> dict[str, Any]:
        points = [
            {
                "timestamp": (NOW - dt.timedelta(minutes=len(self.values) - index)).isoformat(),
                "value": value,
            }
            for index, value in enumerate(self.values)
        ]
        return {
            "service": service,
            "metric": metric,
            "points": points,
            "window_start": points[0]["timestamp"],
            "window_end": points[-1]["timestamp"],
            "simulated": True,
        }


def hours_of(baseline: float, count: int = 24, jitter: float = 0.0) -> list[float]:
    return [baseline + (jitter if index % 2 else -jitter) for index in range(count)]


# --------------------------------------------------------------------- baselines


def points(values: list[float]) -> list[SeriesPoint]:
    return [
        SeriesPoint(timestamp=NOW - dt.timedelta(minutes=len(values) - index), value=value)
        for index, value in enumerate(values)
    ]


def test_baseline_is_median_based_and_resists_outliers() -> None:
    polluted = [*hours_of(0.012, count=20, jitter=0.0005), 0.9]  # one spike must not move it
    baseline = compute_baseline(points(polluted))
    assert 0.010 < baseline.median < 0.014


def test_split_window_keeps_a_recent_slice() -> None:
    values = points([float(index) for index in range(20)])
    older, recent = split_window(values, recent_fraction=0.2, min_recent=3)
    assert len(recent) >= 3
    assert recent[-1].value == 19.0
    assert older[-1].value < recent[0].value


def test_summarise_uses_the_median_not_the_peak() -> None:
    assert summarise(points([0.01, 0.011, 0.6])) == pytest.approx(0.011)


def test_robust_z_is_infinite_when_scale_collapses() -> None:
    # A constant series must not produce a division-by-zero or a "0 sigma means healthy".
    from app.detection.baselines import Baseline

    flat = Baseline(median=0.01, mad=0.0, sigma=0.0, sample_count=10, minimum=0.01, maximum=0.01)
    assert robust_z(0.5, flat) == float("inf")
    assert robust_z(0.01, flat) == 0.0


# ------------------------------------------------------------------- thresholds


def test_metric_family_mapping_covers_every_detected_metric() -> None:
    for metric, family in [
        ("error_rate", MetricFamily.ERROR_RATE),
        ("latency_p95", MetricFamily.LATENCY_P95),
        ("request_rate", MetricFamily.REQUEST_RATE),
        ("payment_failure_rate", MetricFamily.PAYMENT_FAILURE_RATE),
    ]:
        assert family_from_metric(metric) is family


def test_severity_is_tabular_and_explained() -> None:
    severity, explanation = severity_from_signal(
        MetricFamily.ERROR_RATE, observed=0.18, relative_deviation=14.0, z_score=90.0
    )
    assert severity is Severity.SEV2
    assert "0.08" in explanation  # the threshold that decided it


# --------------------------------------------------------------------- detector


async def test_detector_flags_a_sustained_spike() -> None:
    detector = AnomalyDetector(DetectionThresholds(window_minutes=30, min_samples=8))
    values = [*hours_of(0.012, count=20, jitter=0.0004), *[0.18] * 8]
    result = await detector.evaluate_metric(
        FakeMetrics("checkout-service", "error_rate", values), "checkout-service", "error_rate"
    )
    assert result.outcome == "anomaly"
    assert result.is_anomaly
    assert result.severity is Severity.SEV2
    assert result.baseline < 0.02 < result.observed
    assert result.sample_count == len(values)


async def test_detector_reports_insufficient_data_rather_than_healthy() -> None:
    detector = AnomalyDetector(DetectionThresholds(window_minutes=60, min_samples=12))
    result = await detector.evaluate_metric(
        FakeMetrics("checkout-service", "error_rate", [0.01, 0.012]),
        "checkout-service",
        "error_rate",
    )
    assert result.outcome == "insufficient_data"
    assert not result.is_anomaly
    assert (
        "not enough" in result.explanation.lower() or "insufficient" in result.explanation.lower()
    )


async def test_detector_ignores_normal_noise() -> None:
    detector = AnomalyDetector(DetectionThresholds(window_minutes=30, min_samples=8))
    values = [*hours_of(0.012, count=24, jitter=0.0006), *[0.013] * 8]
    result = await detector.evaluate_metric(
        FakeMetrics("checkout-service", "error_rate", values), "checkout-service", "error_rate"
    )
    assert result.outcome == "normal"


async def test_detection_is_deterministic() -> None:
    detector = AnomalyDetector(DetectionThresholds(window_minutes=30, min_samples=8))
    values = [*hours_of(0.012, count=20, jitter=0.0004), *[0.18] * 8]
    first = await detector.evaluate_metric(
        FakeMetrics("checkout-service", "error_rate", values), "checkout-service", "error_rate"
    )
    second = await detector.evaluate_metric(
        FakeMetrics("checkout-service", "error_rate", values), "checkout-service", "error_rate"
    )
    assert (first.outcome, first.severity, first.baseline, first.observed) == (
        second.outcome,
        second.severity,
        second.baseline,
        second.observed,
    )


# -------------------------------------------------------------- incident factory


async def test_incident_type_matches_the_metric_family() -> None:
    detector = AnomalyDetector(DetectionThresholds(window_minutes=30, min_samples=8))
    result = await detector.evaluate_metric(
        FakeMetrics(
            "checkout-service",
            "latency_p95",
            [*hours_of(180.0, count=20, jitter=2.0), *[940.0] * 8],
        ),
        "checkout-service",
        "latency_p95",
    )
    values = build_incident(result, dedup_window_minutes=30, now=NOW)
    assert values["incident_type"] == IncidentType.API_LATENCY_SPIKE.value
    assert "latency" in values["title"].lower()
    assert values["severity"] == result.severity.value


async def test_dedup_key_is_time_bucketed_so_a_sustained_fault_makes_one_incident() -> None:
    detector = AnomalyDetector(DetectionThresholds(window_minutes=30, min_samples=8))
    values = [*hours_of(0.012, count=20, jitter=0.0004), *[0.18] * 8]
    result = await detector.evaluate_metric(
        FakeMetrics("checkout-service", "error_rate", values), "checkout-service", "error_rate"
    )
    inside = build_dedup_key(result, window_minutes=30, now=NOW)
    same_bucket = build_dedup_key(result, window_minutes=30, now=NOW + dt.timedelta(minutes=5))
    next_bucket = build_dedup_key(result, window_minutes=30, now=NOW + dt.timedelta(minutes=45))
    assert inside == same_bucket
    assert inside != next_bucket
