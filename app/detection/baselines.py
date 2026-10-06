"""Baseline statistics for anomaly detection (OPS-030).

Why robust statistics instead of mean/standard deviation:

* a baseline window that contains the *start* of an incident would inflate a plain standard
  deviation and hide the very anomaly we are looking for;
* **median** and **median absolute deviation (MAD)** tolerate up to ~50% contamination, which
  is precisely the situation ("the last few minutes are the anomaly");
* MAD is scaled by 1.4826 so the result is comparable to a standard deviation for normal data.

Everything here is a pure function of its inputs — no clock, no randomness, no I/O — which is
what makes detection behaviour reproducible in tests and in evaluation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

MIN_SIGMA_FLOOR_RATIO = 0.01
"""When MAD is zero (a perfectly flat series), use 1% of the baseline as sigma.

Without a floor, a flat-but-slightly-noisy series produces a division by ~0 and every tiny
blip becomes an "infinite sigma" anomaly. A floor keeps the detector honest: a genuinely
flat series simply cannot produce a large z-score.
"""

MAD_SCALE = 1.4826


@dataclass(frozen=True)
class SeriesPoint:
    timestamp: datetime
    value: float
    partial: bool = False


@dataclass(frozen=True)
class Baseline:
    """Robust summary of the pre-anomaly portion of a series."""

    median: float
    mad: float
    sigma: float
    sample_count: int
    minimum: float
    maximum: float

    @property
    def has_signal(self) -> bool:
        """True when the series varies enough for a z-score to mean anything."""
        return self.sigma > 0


def median(values: list[float]) -> float:
    if not values:
        raise ValueError("median of an empty sequence is undefined")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2.0


def median_absolute_deviation(values: list[float], centre: float | None = None) -> float:
    if not values:
        raise ValueError("MAD of an empty sequence is undefined")
    median_value = median(values) if centre is None else centre
    return median([abs(value - median_value) for value in values])


def compute_baseline(points: list[SeriesPoint]) -> Baseline:
    """Compute a robust baseline from a window of points."""
    values = [point.value for point in points]
    if not values:
        raise ValueError("cannot compute a baseline from an empty series")
    centre = median(values)
    mad = median_absolute_deviation(values, centre)
    sigma = MAD_SCALE * mad
    floor = abs(centre) * MIN_SIGMA_FLOOR_RATIO
    if sigma < floor:
        sigma = floor
    return Baseline(
        median=centre,
        mad=mad,
        sigma=sigma,
        sample_count=len(values),
        minimum=min(values),
        maximum=max(values),
    )


def robust_z(observed: float, baseline: Baseline) -> float:
    if baseline.sigma <= 0:
        return 0.0 if math.isclose(observed, baseline.median) else math.copysign(math.inf, observed)
    return (observed - baseline.median) / baseline.sigma


def relative_deviation(observed: float, baseline: Baseline) -> float:
    """Signed proportional change. Zero baseline is handled explicitly, never divided by."""
    reference = baseline.median
    if math.isclose(reference, 0.0, abs_tol=1e-12):
        return 0.0 if math.isclose(observed, 0.0, abs_tol=1e-12) else math.inf
    return (observed - reference) / abs(reference)


def split_window(
    points: list[SeriesPoint], recent_fraction: float = 0.2, min_recent: int = 3
) -> tuple[list[SeriesPoint], list[SeriesPoint]]:
    """Split into (baseline, recent) portions.

    The recent portion must contain at least ``min_recent`` points so that a single noisy
    sample cannot define the "observed" state, and the baseline must keep at least
    ``min_recent`` points so early-window spikes do not hollow it out.
    """
    if len(points) < min_recent * 2:
        return points, []
    recent_count = max(min_recent, math.ceil(len(points) * recent_fraction))
    recent_count = min(recent_count, len(points) - min_recent)
    return points[:-recent_count], points[-recent_count:]


def summarise(observed_points: list[SeriesPoint]) -> float:
    """Robust summary of the observed window: the median, not the maximum.

    Using the maximum would turn a single retry blip into an incident; the median asks
    "what is the state now?" rather than "what was the worst instant?".
    """
    return median([point.value for point in observed_points])


def peaked(observed_points: list[SeriesPoint]) -> float:
    """The extreme value in the window — reported alongside the median for context."""
    values = [point.value for point in observed_points]
    return max(values, key=abs) if values else 0.0


__all__ = [
    "MAD_SCALE",
    "MIN_SIGMA_FLOOR_RATIO",
    "Baseline",
    "SeriesPoint",
    "compute_baseline",
    "median",
    "median_absolute_deviation",
    "peaked",
    "relative_deviation",
    "robust_z",
    "split_window",
    "summarise",
]
