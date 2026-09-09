"""Declarative data quality checks.

These are the *known* failure modes: a missing partition, a null key, a duplicate
load, a renamed column, an orphan foreign key. They are cheap, deterministic and
explainable, and they are evaluated purely from the metric vector - which means
a check never re-scans the warehouse and can be reproduced from stored metrics
long after the run.

What they cannot catch is the failure nobody wrote a rule for: half the stores
missing, or a measure that quietly changed unit. That is the anomaly detector's
job (``sentinel.detection``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from sentinel.elt.schema import NOT_NULL_COLUMNS
from sentinel.elt.warehouse import Warehouse
from sentinel.generator.source import DATASETS
from sentinel.quality import metrics as m


class Severity(StrEnum):
    """How badly a failed check should be treated."""

    CRITICAL = "critical"
    WARNING = "warning"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check for one dataset on one business date."""

    dataset: str
    check_name: str
    severity: Severity
    passed: bool
    observed: float | None
    threshold: float | None
    details: str

    def as_dict(self) -> dict[str, object]:
        """Serialisable form, used in incident documents and agent prompts."""
        return {
            "dataset": self.dataset,
            "check": self.check_name,
            "severity": str(self.severity),
            "passed": self.passed,
            "observed": self.observed,
            "threshold": self.threshold,
            "details": self.details,
        }


@dataclass(frozen=True)
class CheckDefinition:
    """A metric-level assertion."""

    name: str
    metric: str
    severity: Severity
    threshold: float
    comparison: str
    """One of ``le`` (observed <= threshold) or ``ge`` (observed >= threshold)."""

    message: str

    def evaluate(self, dataset: str, observed: float | None) -> CheckResult:
        """Compare an observation against the threshold."""
        if observed is None:
            return CheckResult(
                dataset=dataset,
                check_name=self.name,
                severity=self.severity,
                passed=False,
                observed=None,
                threshold=self.threshold,
                details=f"metric {self.metric!r} was not produced for this run",
            )
        passed = (
            observed <= self.threshold if self.comparison == "le" else observed >= self.threshold
        )
        return CheckResult(
            dataset=dataset,
            check_name=self.name,
            severity=self.severity,
            passed=passed,
            observed=observed,
            threshold=self.threshold,
            details=self.message if not passed else "ok",
        )


def definitions_for(dataset: str) -> list[CheckDefinition]:
    """Build the check suite for a dataset."""
    checks: list[CheckDefinition] = [
        CheckDefinition(
            name="partition_present",
            metric=m.ARRIVED,
            severity=Severity.CRITICAL,
            threshold=1.0,
            comparison="ge",
            message="no extract arrived for this business date",
        ),
        CheckDefinition(
            name="schema_complete",
            metric=m.MISSING_COLUMN_COUNT,
            severity=Severity.CRITICAL,
            threshold=0.0,
            comparison="le",
            message="one or more expected columns are absent from the extract",
        ),
        CheckDefinition(
            name="unique_natural_key",
            metric=m.DUPLICATE_RATE,
            severity=Severity.CRITICAL,
            threshold=0.0,
            comparison="le",
            message="the natural key is not unique in this partition",
        ),
        CheckDefinition(
            name="referential_integrity_sku",
            metric=m.UNKNOWN_KEY_RATE,
            severity=Severity.CRITICAL,
            threshold=0.0,
            comparison="le",
            message="rows reference SKUs that are absent from the product master",
        ),
    ]
    checks.extend(
        CheckDefinition(
            name=f"not_null_{column}",
            metric=f"{m.NULL_RATE_PREFIX}{column}",
            severity=Severity.CRITICAL,
            threshold=0.0,
            comparison="le",
            message=f"column {column!r} contains nulls but is declared mandatory",
        )
        for column in NOT_NULL_COLUMNS[dataset]
    )
    checks.extend(
        CheckDefinition(
            name=f"non_negative_{measure}",
            metric=f"{m.MEASURE_SUM_PREFIX}{measure}",
            severity=Severity.WARNING,
            threshold=0.0,
            comparison="ge",
            message=f"measure {measure!r} summed to a negative value",
        )
        for measure in DATASETS[dataset].measures
    )
    return checks


def run_checks(
    dataset: str,
    metric_values: dict[str, float | None],
) -> list[CheckResult]:
    """Evaluate every check for a dataset against a metric vector."""
    return [
        definition.evaluate(dataset, metric_values.get(definition.metric))
        for definition in definitions_for(dataset)
    ]


def persist_checks(
    warehouse: Warehouse,
    run_id: str,
    business_date: date,
    results: list[CheckResult],
) -> None:
    """Write check results, replacing any earlier run for the same date."""
    for dataset in {result.dataset for result in results}:
        warehouse.execute(
            "DELETE FROM dq_check_result WHERE business_date = ? AND dataset = ?",
            [business_date, dataset],
        )
    evaluated_at = datetime.now(tz=UTC).replace(tzinfo=None)
    warehouse.executemany(
        "INSERT INTO dq_check_result VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                business_date,
                result.dataset,
                result.check_name,
                str(result.severity),
                result.passed,
                result.observed,
                result.threshold,
                result.details,
                evaluated_at,
            )
            for result in results
        ],
    )


def failed(results: list[CheckResult], severity: Severity | None = None) -> list[CheckResult]:
    """Filter to failing checks, optionally of one severity."""
    return [
        result
        for result in results
        if not result.passed and (severity is None or result.severity is severity)
    ]
