"""Detection thresholds — configuration with defaults that are explicit, not magic.

Thresholds live in one dataclass so they can be tuned from the environment, printed into the
detection record (so a human can see *why* something fired) and swept in the evaluation
harness.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings


@dataclass(frozen=True)
class DetectionThresholds:
    """Guard rails applied to every metric family."""

    window_minutes: int = 60
    min_samples: int = 12
    z_threshold: float = 4.0
    min_relative_deviation: float = 0.5
    recent_fraction: float = 0.2
    min_recent: int = 3
    include_partial_point: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> DetectionThresholds:
        return cls(
            window_minutes=settings.detection_window_minutes,
            min_samples=settings.detection_min_samples,
            z_threshold=settings.detection_z_threshold,
            min_relative_deviation=settings.detection_min_relative_deviation,
        )

    def describe(self) -> dict[str, float | int | bool]:
        return {
            "window_minutes": self.window_minutes,
            "min_samples": self.min_samples,
            "z_threshold": self.z_threshold,
            "min_relative_deviation": self.min_relative_deviation,
            "recent_fraction": self.recent_fraction,
            "min_recent": self.min_recent,
        }


#: Metric families the detector treats as *impact* metrics, where the absolute level matters
#: as much as the deviation (18% errors is a problem even if it is "only" 3 sigma from a
#: noisy baseline).
IMPACT_FAMILIES = frozenset({"error_rate", "payment_failure_rate"})

#: Metrics where a *decrease* is the worrying direction (traffic collapse).
DOWNWARD_IS_BAD = frozenset({"request_rate"})

#: Minimum samples per metric family override — sparse series need more history to be trusted.
MIN_SAMPLES_OVERRIDE: dict[str, int] = {
    "payment_failure_rate": 8,
    "saturation": 10,
}


__all__ = [
    "DOWNWARD_IS_BAD",
    "IMPACT_FAMILIES",
    "MIN_SAMPLES_OVERRIDE",
    "DetectionThresholds",
]
