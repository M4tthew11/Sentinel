"""Data quality: metrics, declarative checks and row-level contracts."""

from sentinel.quality.checks import CheckResult, Severity, persist_checks, run_checks
from sentinel.quality.contracts import CONTRACTS, validate_sample
from sentinel.quality.metrics import (
    MetricRecord,
    collect_metrics,
    metric_history,
    metrics_for_run,
    persist_metrics,
)

__all__ = [
    "CONTRACTS",
    "CheckResult",
    "MetricRecord",
    "Severity",
    "collect_metrics",
    "metric_history",
    "metrics_for_run",
    "persist_checks",
    "persist_metrics",
    "run_checks",
    "validate_sample",
]
