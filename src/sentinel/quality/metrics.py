"""Pipeline metrics.

Every run emits the same vector of metrics per dataset. Those vectors are the
substrate for everything downstream: the declarative checks compare them against
fixed thresholds, and the anomaly detector compares them against their own
history. Metric names are stable identifiers, because a renamed metric silently
resets a model's history.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pandas as pd

from sentinel.elt import ops
from sentinel.elt.schema import NOT_NULL_COLUMNS
from sentinel.elt.warehouse import Warehouse
from sentinel.generator.source import DATASETS
from sentinel.logging import get_logger

log = get_logger(__name__)

#: Metrics whose absolute level is meaningful across datasets.
ROW_COUNT = "row_count"
ARRIVED = "arrived"
DUPLICATE_RATE = "duplicate_rate"
UNKNOWN_KEY_RATE = "unknown_key_rate"
MISSING_COLUMN_COUNT = "missing_column_count"
REJECTED_RATE = "rejected_rate"

NULL_RATE_PREFIX = "null_rate__"
MEASURE_SUM_PREFIX = "measure_sum__"
MEASURE_AVG_PREFIX = "measure_avg__"


@dataclass(frozen=True)
class MetricRecord:
    """One metric observation for one dataset on one business date."""

    dataset: str
    metric: str
    value: float | None


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


def _partition_relation(dataset: str) -> str:
    """SQL for every projected row of a partition, valid and rejected alike.

    Metrics must see the rows that were thrown away, otherwise switching on
    ``quarantine_invalid_rows`` would make a broken feed look healthy.
    """
    return f"""
        SELECT * FROM silver_{dataset} WHERE _business_date = ?
        UNION ALL
        SELECT * FROM silver_{dataset}_rejected WHERE _business_date = ?
    """


def collect_metrics(
    warehouse: Warehouse,
    dataset: str,
    business_date: date,
) -> list[MetricRecord]:
    """Compute the metric vector for one dataset on one business date."""
    spec = DATASETS[dataset]
    records: list[MetricRecord] = []

    bronze_rows = int(
        warehouse.scalar(
            f"SELECT count(*) FROM bronze_{dataset} WHERE _business_date = ?", [business_date]
        )
        or 0
    )
    records.append(MetricRecord(dataset, ROW_COUNT, float(bronze_rows)))
    records.append(MetricRecord(dataset, ARRIVED, 1.0 if bronze_rows > 0 else 0.0))

    payload_columns = set(_payload_keys(warehouse, dataset, business_date))
    mapping = ops.column_mapping(warehouse, dataset)
    missing = [
        canonical
        for canonical, candidates in mapping.items()
        if payload_columns and not any(source in payload_columns for source in candidates)
    ]
    records.append(MetricRecord(dataset, MISSING_COLUMN_COUNT, float(len(missing))))

    if bronze_rows == 0:
        # A missing partition still emits the full vector, with zeros, so the
        # metric time series never has holes the detector would have to guess at.
        for column in NOT_NULL_COLUMNS[dataset]:
            records.append(MetricRecord(dataset, f"{NULL_RATE_PREFIX}{column}", 0.0))
        for measure in spec.measures:
            records.append(MetricRecord(dataset, f"{MEASURE_SUM_PREFIX}{measure}", 0.0))
            records.append(MetricRecord(dataset, f"{MEASURE_AVG_PREFIX}{measure}", 0.0))
        records.append(MetricRecord(dataset, DUPLICATE_RATE, 0.0))
        records.append(MetricRecord(dataset, UNKNOWN_KEY_RATE, 0.0))
        records.append(MetricRecord(dataset, REJECTED_RATE, 0.0))
        return records

    relation = _partition_relation(dataset)
    params = [business_date, business_date]

    null_selects = ",\n            ".join(
        f"avg(CASE WHEN {column} IS NULL THEN 1.0 ELSE 0.0 END) AS null_rate_{column}"
        for column in NOT_NULL_COLUMNS[dataset]
    )
    measure_selects = ",\n            ".join(
        f"sum({measure}) AS sum_{measure}, avg({measure}) AS avg_{measure}"
        for measure in spec.measures
    )
    key_columns = ", ".join(spec.natural_key)

    frame = warehouse.fetch_df(
        f"""
        WITH partition_rows AS ({relation})
        SELECT
            count(*)                                     AS total_rows,
            count(DISTINCT ({key_columns}))              AS distinct_keys,
            avg(
                CASE
                    -- a null key is a null-rate problem, not an orphan
                    WHEN sku_id IS NULL THEN 0.0
                    WHEN sku_id IN (SELECT sku_id FROM dim_sku) THEN 0.0
                    ELSE 1.0
                END
            )                                            AS unknown_key_rate,
            {null_selects},
            {measure_selects}
        FROM partition_rows
        """,
        params,
    )
    row = frame.iloc[0]
    total = float(row["total_rows"]) or 1.0
    distinct = float(row["distinct_keys"])
    records.append(MetricRecord(dataset, DUPLICATE_RATE, round(1.0 - distinct / total, 6)))
    records.append(
        MetricRecord(dataset, UNKNOWN_KEY_RATE, _as_float(row["unknown_key_rate"]) or 0.0)
    )
    for column in NOT_NULL_COLUMNS[dataset]:
        records.append(
            MetricRecord(
                dataset, f"{NULL_RATE_PREFIX}{column}", _as_float(row[f"null_rate_{column}"])
            )
        )
    for measure in spec.measures:
        records.append(
            MetricRecord(
                dataset, f"{MEASURE_SUM_PREFIX}{measure}", _as_float(row[f"sum_{measure}"])
            )
        )
        records.append(
            MetricRecord(
                dataset, f"{MEASURE_AVG_PREFIX}{measure}", _as_float(row[f"avg_{measure}"])
            )
        )

    rejected = float(
        warehouse.scalar(
            f"SELECT count(*) FROM silver_{dataset}_rejected WHERE _business_date = ?",
            [business_date],
        )
        or 0
    )
    records.append(MetricRecord(dataset, REJECTED_RATE, round(rejected / total, 6)))
    return records


def _payload_keys(warehouse: Warehouse, dataset: str, business_date: date) -> list[str]:
    row = warehouse.fetch_one(
        f"SELECT json_keys(payload) FROM bronze_{dataset} WHERE _business_date = ? LIMIT 1",
        [business_date],
    )
    if row is None or row[0] is None:
        return []
    return [str(key) for key in row[0]]


def _as_float(value: object) -> float | None:
    """Convert a DuckDB/pandas scalar to a plain float, mapping NULL/NaN to None."""
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def persist_metrics(
    warehouse: Warehouse,
    run_id: str,
    business_date: date,
    records: list[MetricRecord],
) -> None:
    """Write a metric vector to ``dq_metric``, replacing any earlier run for the date."""
    datasets = {record.dataset for record in records}
    for dataset in datasets:
        warehouse.execute(
            "DELETE FROM dq_metric WHERE business_date = ? AND dataset = ?",
            [business_date, dataset],
        )
    computed_at = _now()
    warehouse.executemany(
        "INSERT INTO dq_metric VALUES (?, ?, ?, ?, ?, ?)",
        [
            (run_id, business_date, record.dataset, record.metric, record.value, computed_at)
            for record in records
        ],
    )
    log.debug("metrics.persisted", business_date=str(business_date), count=len(records))


def metrics_for_run(
    warehouse: Warehouse, business_date: date, dataset: str | None = None
) -> dict[str, dict[str, float | None]]:
    """Return ``{dataset: {metric: value}}`` for one business date."""
    sql = "SELECT dataset, metric, value FROM dq_metric WHERE business_date = ?"
    params: list[object] = [business_date]
    if dataset:
        sql += " AND dataset = ?"
        params.append(dataset)
    result: dict[str, dict[str, float | None]] = {}
    for row in warehouse.fetch_records(sql, params):
        result.setdefault(str(row["dataset"]), {})[str(row["metric"])] = _as_float(row["value"])
    return result


def metric_history(
    warehouse: Warehouse,
    dataset: str,
    before_date: date,
    lookback_days: int = 60,
) -> pd.DataFrame:
    """Return a wide metric history for a dataset, strictly before ``before_date``.

    Columns are metrics, the index is the business date. Excluding the current
    date is what keeps the detector honest: a run is always scored against the
    past, never against itself.
    """
    frame = warehouse.fetch_df(
        """
        SELECT business_date, metric, value
        FROM dq_metric
        WHERE dataset = ?
          AND business_date < ?
          AND business_date >= (? - INTERVAL 1 DAY * ?)
        """,
        [dataset, before_date, before_date, lookback_days],
    )
    if frame.empty:
        return pd.DataFrame()
    wide = frame.pivot_table(index="business_date", columns="metric", values="value")
    return wide.sort_index()


def latest_metrics(warehouse: Warehouse, dataset: str, business_date: date) -> dict[str, float]:
    """Return the metric vector of one dataset on one date as a plain dict."""
    rows = warehouse.fetch_records(
        "SELECT metric, value FROM dq_metric WHERE dataset = ? AND business_date = ?",
        [dataset, business_date],
    )
    values = ((str(row["metric"]), _as_float(row["value"])) for row in rows)
    return {metric: value for metric, value in values if value is not None}
