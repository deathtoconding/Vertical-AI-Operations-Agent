"""Detection: deterministic statistics, not model intuition."""

from app.detection.baselines import (
    Baseline,
    SeriesPoint,
    compute_baseline,
    median,
    robust_z,
    split_window,
)
from app.detection.detector import DEFAULT_METRICS, AnomalyDetector
from app.detection.incident_factory import build_incident, build_incident_from_detection
from app.detection.severity import family_from_metric, severity_from_signal
from app.detection.thresholds import DetectionThresholds

__all__ = [
    "DEFAULT_METRICS",
    "AnomalyDetector",
    "Baseline",
    "DetectionThresholds",
    "SeriesPoint",
    "build_incident",
    "build_incident_from_detection",
    "compute_baseline",
    "family_from_metric",
    "median",
    "robust_z",
    "severity_from_signal",
    "split_window",
]
