"""Incident construction and deduplication (OPS-031).

Detection produces a *statistical* verdict; this layer turns it into an incident a human can
work. The two failure modes the tests attack are the ones that make an on-call rotation hate
the system: an incident storm (one fault, many pages) and a silent drop (a worsening fault
that is attributed to nothing).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.detection.incident_factory import (
    METRIC_TO_INCIDENT_TYPE,
    TITLE_TEMPLATES,
    build_anomaly_values,
    build_dedup_key,
    build_incident,
    build_incident_from_detection,
    dedup_cutoff,
    dedup_window_seconds,
    severity_rank,
)
from app.domain.enums import IncidentStatus, IncidentType, MetricFamily, Severity
from app.domain.incidents import DetectionResult

pytestmark = [pytest.mark.story("OPS-031"), pytest.mark.unit]

NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)


def detection(**overrides: object) -> DetectionResult:
    values: dict[str, object] = {
        "metric": "error_rate",
        "family": MetricFamily.ERROR_RATE,
        "service": "checkout-service",
        "baseline": 0.012,
        "observed": 0.184,
        "deviation": 0.172,
        "z_score": 121.0,
        "relative_deviation": 14.3,
        "sample_count": 30,
        "severity": Severity.SEV2,
        "detected_at": NOW,
        "is_anomaly": True,
        "outcome": "anomaly",
        "window_start": NOW - dt.timedelta(minutes=30),
        "window_end": NOW,
        "explanation": "error_rate is 14.3x its baseline (robust z=121.0)",
        "detail": {"thresholds": {"z": 4.0}},
    }
    values.update(overrides)
    return DetectionResult.model_validate(values)


# --------------------------------------------------------------------------- #
# Metric -> incident type
# --------------------------------------------------------------------------- #


def test_every_metric_family_maps_to_a_working_incident_type() -> None:
    for family in MetricFamily:
        incident_type = METRIC_TO_INCIDENT_TYPE.get(family)
        assert incident_type is not None, f"{family} has no incident type mapping"
        # A type without a title template would fail at incident creation time, not at review.
        assert incident_type in TITLE_TEMPLATES


def test_latency_metrics_never_become_error_spike_incidents() -> None:
    """The playbook follows the metric, not the other way round."""
    assert METRIC_TO_INCIDENT_TYPE[MetricFamily.LATENCY_P95] is IncidentType.API_LATENCY_SPIKE
    assert (
        METRIC_TO_INCIDENT_TYPE[MetricFamily.PAYMENT_FAILURE_RATE]
        is IncidentType.SUBSCRIPTION_PAYMENT_ANOMALY
    )
    assert METRIC_TO_INCIDENT_TYPE[MetricFamily.REQUEST_RATE] is IncidentType.TRAFFIC_ANOMALY


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #


def test_dedup_key_is_stable_inside_one_window_and_changes_across_windows() -> None:
    series = detection()
    first = build_dedup_key(series, window_minutes=30, now=NOW)
    same_bucket = build_dedup_key(series, window_minutes=30, now=NOW + dt.timedelta(seconds=90))
    next_bucket = build_dedup_key(series, window_minutes=30, now=NOW + dt.timedelta(minutes=30))

    assert first == same_bucket, "a detection 90s later must not page twice"
    assert first != next_bucket, "a still-firing fault must be re-evaluated after the window"


def test_dedup_key_separates_service_metric_and_direction() -> None:
    base = build_dedup_key(detection(), window_minutes=30, now=NOW)
    other_service = build_dedup_key(detection(service="payments-api"), window_minutes=30, now=NOW)
    other_metric = build_dedup_key(detection(metric="latency_p95"), window_minutes=30, now=NOW)
    dropping = build_dedup_key(detection(relative_deviation=-0.9), window_minutes=30, now=NOW)

    assert len({base, other_service, other_metric, dropping}) == 4
    assert len(base) == 40, "keys are hashed, not raw identifiers"


def test_dedup_window_has_a_one_minute_floor() -> None:
    assert dedup_window_seconds(0) == 60
    assert dedup_window_seconds(1) == 60
    assert dedup_window_seconds(30) == 1800


def test_dedup_cutoff_is_one_window_before_now() -> None:
    assert dedup_cutoff(30, now=NOW) == NOW - dt.timedelta(minutes=30)


# --------------------------------------------------------------------------- #
# Incident values
# --------------------------------------------------------------------------- #


def test_incident_is_created_from_detection_with_its_context() -> None:
    values = build_incident(detection(), dedup_window_minutes=30, now=NOW)

    assert values["status"] == IncidentStatus.OPEN.value
    assert values["severity"] == Severity.SEV2.value
    assert values["incident_type"] == IncidentType.API_ERROR_SPIKE.value
    assert values["service"] == "checkout-service"
    assert values["metric"] == "error_rate"
    assert values["detected_at"] == NOW
    assert "checkout-service" in values["title"]
    assert "18.4% vs baseline 1.2%" in values["title"], "percentages must be human-readable"
    assert "robust z=121.0" in values["summary"]
    assert values["details"]["detection"]["baseline"] == pytest.approx(0.012)


def test_incident_title_uses_the_metric_units() -> None:
    latency = build_incident(
        detection(
            metric="latency_p95",
            family=MetricFamily.LATENCY_P95,
            baseline=180.0,
            observed=940.0,
            severity=Severity.SEV2,
        ),
        dedup_window_minutes=30,
        now=NOW,
    )
    assert latency["incident_type"] == IncidentType.API_LATENCY_SPIKE.value
    assert "940ms vs baseline 180ms" in latency["title"]


def test_simulated_flag_is_carried_onto_the_incident() -> None:
    values = build_incident(detection(), dedup_window_minutes=30, now=NOW, simulated=True)
    assert values["simulated"] is True


def test_infinite_z_score_is_not_persisted_as_infinity() -> None:
    """``inf`` breaks JSON serialisation downstream; ``null`` means "unknown", which is honest."""
    values = build_incident(detection(z_score=float("inf")), dedup_window_minutes=30, now=NOW)
    assert values["details"]["detection"]["z_score"] is None


def test_existing_incident_marks_the_detection_as_a_duplicate_not_a_new_incident() -> None:
    values, duplicate = build_incident_from_detection(
        detection(), dedup_window_minutes=30, existing=object(), now=NOW
    )
    assert duplicate is True
    # The values are still built: a worsening fault must be recorded even when it does not page.
    assert values["dedup_key"]

    _, fresh = build_incident_from_detection(detection(), dedup_window_minutes=30, now=NOW)
    assert fresh is False


def test_anomaly_values_keep_the_detection_even_without_an_incident() -> None:
    values = build_anomaly_values(detection(), incident_id=None, deduplicated=True, simulated=False)
    assert values["incident_id"] is None
    assert values["deduplicated"] is True
    assert values["z_score"] == 121.0
    assert values["details"]["outcome"] == "anomaly"


def test_anomaly_values_clamp_infinite_z_score_for_the_database_column() -> None:
    values = build_anomaly_values(
        detection(z_score=float("inf")), incident_id=None, deduplicated=False, simulated=False
    )
    assert values["z_score"] == 9999.0


def test_severity_rank_orders_the_way_incidents_are_triaged() -> None:
    """SEV1 is the *highest* priority, so it carries the lowest rank number."""
    ranks = {severity: severity_rank(severity) for severity in Severity}
    assert ranks[Severity.SEV1] == 1
    assert len(set(ranks.values())) == len(ranks), "ranks must be unique, not a tie-break lottery"
    triage_order = sorted(Severity, key=severity_rank)
    assert triage_order[:2] == [Severity.SEV1, Severity.SEV2]
    assert severity_rank(Severity.SEV1) < severity_rank(Severity.SEV3)
