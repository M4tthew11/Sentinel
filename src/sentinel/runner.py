"""One observed pipeline run: transform, measure, check, score.

Kept separate from :mod:`sentinel.monitor` so that the remediation executor can
re-run and re-observe a business date (to verify its own fix) without importing
the incident-handling loop that invokes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sentinel.config import Settings, get_settings
from sentinel.detection.detector import DetectionResult, detect_for_run
from sentinel.elt.pipeline import PipelineResult, run_pipeline
from sentinel.elt.warehouse import Warehouse
from sentinel.generator.source import DATASETS
from sentinel.logging import get_logger
from sentinel.quality import metrics as m
from sentinel.quality.checks import CheckResult, Severity, persist_checks, run_checks

log = get_logger(__name__)


@dataclass
class RunObservation:
    """Everything observed about one business date after a pipeline run."""

    business_date: date
    pipeline: PipelineResult
    metrics: dict[str, dict[str, float | None]] = field(default_factory=dict)
    checks: dict[str, list[CheckResult]] = field(default_factory=dict)
    detections: dict[str, DetectionResult] = field(default_factory=dict)

    def failing_checks(self, dataset: str) -> list[CheckResult]:
        """Failed checks for one dataset."""
        return [result for result in self.checks.get(dataset, []) if not result.passed]

    def has_critical_failure(self, dataset: str | None = None) -> bool:
        """Whether any critical check failed, overall or for one dataset."""
        datasets = [dataset] if dataset else list(self.checks)
        return any(
            not result.passed and result.severity is Severity.CRITICAL
            for name in datasets
            for result in self.checks.get(name, [])
        )

    def is_healthy(self, dataset: str | None = None) -> bool:
        """Whether a dataset (or the whole run) is free of critical failures and anomalies."""
        datasets = [dataset] if dataset else list(self.checks)
        if self.has_critical_failure(dataset):
            return False
        return not any(
            self.detections[name].is_anomalous for name in datasets if name in self.detections
        )


def refresh_run(
    business_date: date,
    warehouse: Warehouse,
    settings: Settings | None = None,
    triggered_by: str = "cli",
    detect: bool = True,
) -> RunObservation:
    """Run the pipeline for a date, then recompute metrics, checks and anomalies.

    Args:
        business_date: Date to process.
        warehouse: Open warehouse.
        settings: Settings override.
        triggered_by: Free-text provenance recorded on the run.
        detect: Whether to score anomalies (skipped during fix verification, where
            only the deterministic checks matter).
    """
    settings = settings or get_settings()
    pipeline_result = run_pipeline(business_date, warehouse, settings, triggered_by)
    observation = RunObservation(business_date=business_date, pipeline=pipeline_result)

    for dataset in DATASETS:
        records = m.collect_metrics(warehouse, dataset, business_date)
        m.persist_metrics(warehouse, pipeline_result.run_id, business_date, records)
        values: dict[str, float | None] = {record.metric: record.value for record in records}
        observation.metrics[dataset] = values

        results = run_checks(dataset, values)
        persist_checks(warehouse, pipeline_result.run_id, business_date, results)
        observation.checks[dataset] = results

        if detect:
            observation.detections[dataset] = detect_for_run(
                warehouse, business_date, dataset, settings.detection
            )

    log.info(
        "run.observed",
        business_date=str(business_date),
        run_id=pipeline_result.run_id,
        failed_checks=sum(
            1 for results in observation.checks.values() for r in results if not r.passed
        ),
        anomalous_datasets=[
            name for name, result in observation.detections.items() if result.is_anomalous
        ],
    )
    return observation
