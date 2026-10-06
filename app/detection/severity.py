"""Severity mapping — deterministic, documented, and independent of the model (OPS-030).

The mapping is intentionally simple and auditable: a human reading the incident severity must
be able to reconstruct exactly which number produced it. Model output never sets severity —
an injected instruction cannot promote an incident to SEV1, and an over-cautious model cannot
inflate one either.
"""

from __future__ import annotations

from app.domain.enums import MetricFamily, Severity

#: Absolute level thresholds per family, most severe first. These encode the operational
#: question "at what point are customers actually affected?".
ABSOLUTE_THRESHOLDS: dict[MetricFamily, tuple[tuple[float, Severity], ...]] = {
    MetricFamily.ERROR_RATE: (
        (0.25, Severity.SEV1),
        (0.08, Severity.SEV2),
        (0.03, Severity.SEV3),
    ),
    MetricFamily.PAYMENT_FAILURE_RATE: (
        (0.20, Severity.SEV1),
        (0.05, Severity.SEV2),
        (0.02, Severity.SEV3),
    ),
    MetricFamily.SATURATION: (
        (0.95, Severity.SEV1),
        (0.85, Severity.SEV2),
        (0.75, Severity.SEV3),
    ),
}

#: Relative-change thresholds for latency and traffic families.
LATENCY_RELATIVE_THRESHOLDS: tuple[tuple[float, Severity], ...] = (
    (5.0, Severity.SEV1),
    (2.5, Severity.SEV2),
    (1.5, Severity.SEV3),
)

TRAFFIC_DROP_THRESHOLDS: tuple[tuple[float, Severity], ...] = (
    (0.7, Severity.SEV1),
    (0.5, Severity.SEV2),
    (0.3, Severity.SEV3),
)

#: Fallback when only the statistical signal is available.
Z_FALLBACK_THRESHOLDS: tuple[tuple[float, Severity], ...] = (
    (12.0, Severity.SEV2),
    (8.0, Severity.SEV3),
)


def severity_from_signal(
    family: MetricFamily,
    *,
    observed: float,
    relative_deviation: float,
    z_score: float,
) -> tuple[Severity, str]:
    """Return ``(severity, explanation)`` for one detection result.

    The explanation is stored on the detection event so that "why SEV2?" is answerable from
    the record rather than from somebody's memory.
    """
    if family in ABSOLUTE_THRESHOLDS:
        for level, severity in ABSOLUTE_THRESHOLDS[family]:
            if observed >= level:
                return severity, (
                    f"observed {observed:.4f} is at or above the {level:.2f} {severity.value} "
                    f"threshold for {family.value}"
                )

    if family in {MetricFamily.LATENCY_P95, MetricFamily.LATENCY_P99}:
        for level, severity in LATENCY_RELATIVE_THRESHOLDS:
            if relative_deviation >= level:
                return severity, (
                    f"{family.value} increased {relative_deviation * 100:.0f}% "
                    f"(≥{level * 100:.0f}% is {severity.value})"
                )

    if family in {MetricFamily.REQUEST_RATE} and relative_deviation < 0:
        drop = abs(relative_deviation)
        for level, severity in TRAFFIC_DROP_THRESHOLDS:
            if drop >= level:
                return severity, (
                    f"{family.value} dropped {drop * 100:.0f}% "
                    f"(≥{level * 100:.0f}% is {severity.value})"
                )

    for level, severity in Z_FALLBACK_THRESHOLDS:
        if abs(z_score) >= level:
            return severity, f"robust z-score {z_score:.1f} is at or above {level:.0f}"

    return Severity.SEV4, (
        f"deviation {relative_deviation * 100:.0f}% / z {z_score:.1f} is anomalous but "
        "has limited customer impact"
    )


def family_from_metric(metric: str) -> MetricFamily:
    """Map a metric name onto a family, defaulting to a request-rate-like series."""
    try:
        return MetricFamily(metric)
    except ValueError:
        mapping = {
            "errors": MetricFamily.ERROR_RATE,
            "error_rate_5xx": MetricFamily.ERROR_RATE,
            "p95_latency_ms": MetricFamily.LATENCY_P95,
            "latency_ms": MetricFamily.LATENCY_P95,
            "throughput": MetricFamily.REQUEST_RATE,
            "cpu_saturation": MetricFamily.SATURATION,
        }
        return mapping.get(metric, MetricFamily.REQUEST_RATE)


__all__ = [
    "ABSOLUTE_THRESHOLDS",
    "LATENCY_RELATIVE_THRESHOLDS",
    "TRAFFIC_DROP_THRESHOLDS",
    "family_from_metric",
    "severity_from_signal",
]
