"""Metrics describe the partition; checks turn them into pass/fail verdicts."""

from __future__ import annotations

from datetime import date

from sentinel.elt import ops
from sentinel.elt.pipeline import run_pipeline
from sentinel.generator.faults import FaultSpec, FaultType
from sentinel.quality import metrics as m
from sentinel.quality.checks import Severity, run_checks
from sentinel.quality.contracts import validate_sample
from sentinel.runner import refresh_run


def _metrics(settings, warehouse, business_date, dataset="pos_sales"):
    run_pipeline(business_date, warehouse, settings)
    records = m.collect_metrics(warehouse, dataset, business_date)
    return {record.metric: record.value for record in records}


def test_clean_partition_passes_every_check(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date)
    values = _metrics(settings, warehouse, business_date)

    assert values[m.ROW_COUNT] > 0
    assert values[m.DUPLICATE_RATE] == 0
    assert values[m.UNKNOWN_KEY_RATE] == 0
    assert values[m.MISSING_COLUMN_COUNT] == 0
    assert all(result.passed for result in run_checks("pos_sales", values))


def test_missing_partition_fails_only_the_arrival_check(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("shipments", FaultType.LATE_ARRIVAL))
    values = _metrics(settings, warehouse, business_date, "shipments")

    failed = [result.check_name for result in run_checks("shipments", values) if not result.passed]
    assert failed == ["partition_present"]


def test_duplicate_batch_fails_uniqueness(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("shipments", FaultType.DUPLICATE_BATCH, magnitude=0.4))
    values = _metrics(settings, warehouse, business_date, "shipments")

    assert values[m.DUPLICATE_RATE] > 0
    failed = [result.check_name for result in run_checks("shipments", values) if not result.passed]
    assert "unique_natural_key" in failed


def test_null_spike_fails_the_mandatory_column(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("inventory", FaultType.NULL_SPIKE, magnitude=0.3))
    values = _metrics(settings, warehouse, business_date, "inventory")

    assert values[f"{m.NULL_RATE_PREFIX}on_hand_cases"] > 0.2
    failed = [result.check_name for result in run_checks("inventory", values) if not result.passed]
    assert "not_null_on_hand_cases" in failed


def test_schema_drift_fails_completeness(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("inventory", FaultType.SCHEMA_DRIFT))
    values = _metrics(settings, warehouse, business_date, "inventory")

    assert values[m.MISSING_COLUMN_COUNT] == 1
    failed = [result.check_name for result in run_checks("inventory", values) if not result.passed]
    assert "schema_complete" in failed


def test_quarantined_rows_still_count_towards_metrics(settings, warehouse, inject) -> None:
    """Switching on quarantine must not make a broken feed look healthy."""
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("pos_sales", FaultType.REFERENTIAL_BREAK, magnitude=0.2))
    before = _metrics(settings, warehouse, business_date)

    ops.set_flag(warehouse, "pos_sales", "quarantine_invalid_rows", "true", "test")
    after = _metrics(settings, warehouse, business_date)

    assert after[m.UNKNOWN_KEY_RATE] == before[m.UNKNOWN_KEY_RATE] > 0
    assert after[m.REJECTED_RATE] > 0
    assert after[m.ROW_COUNT] == before[m.ROW_COUNT]
    assert after[m.DUPLICATE_RATE] == before[m.DUPLICATE_RATE] == 0, (
        "quarantined rows must be counted once, not once in silver and once in rejected"
    )


def test_metrics_are_persisted_and_queryable(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date)
    refresh_run(business_date, warehouse, settings, "test", detect=False)

    stored = m.metrics_for_run(warehouse, business_date)
    assert set(stored) == {"pos_sales", "shipments", "inventory"}
    assert stored["pos_sales"][m.ROW_COUNT] > 0

    history = m.metric_history(warehouse, "pos_sales", business_date)
    assert history.empty, "history must exclude the date being scored"


def test_check_severity_is_declared() -> None:
    values = {m.ARRIVED: 0.0, m.MISSING_COLUMN_COUNT: 0.0}
    results = {result.check_name: result for result in run_checks("pos_sales", values)}
    assert results["partition_present"].severity is Severity.CRITICAL
    assert results["non_negative_units_sold"].severity is Severity.WARNING


def test_contracts_report_readable_violations() -> None:
    violations = validate_sample(
        "pos_sales",
        [
            {
                "sale_date": "2025-03-01",
                "store_id": "CUST-01-S001",
                "customer_id": "CUST-01",
                "sku_id": "NOT-A-SKU",
                "units_sold": -3,
                "net_sales_usd": 10.0,
                "promo_flag": "N",
            }
        ],
    )
    fields = {violation.field for violation in violations}
    assert fields == {"sku_id", "units_sold"}
