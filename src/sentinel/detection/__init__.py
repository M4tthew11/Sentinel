"""ML anomaly detection over pipeline metrics."""

from sentinel.detection.detector import (
    AnomalySignal,
    DetectionResult,
    detect_anomalies,
    detect_for_run,
)

__all__ = ["AnomalySignal", "DetectionResult", "detect_anomalies", "detect_for_run"]
