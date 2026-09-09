"""The medallion pipeline: landing -> bronze -> silver -> gold.

Design notes
------------
*Bronze* is append-only and schema-agnostic (JSON payload + load metadata), so a
source schema change never breaks ingestion - it becomes an observable fact.

*Silver* is a full rebuild from bronze on every run. That is what makes
remediation cheap: an agent changes a column mapping or sets a flag, the next
rebuild simply produces different (correct) data, and the fix is reproducible
from bronze rather than being a manual UPDATE nobody can audit.

*Gold* is the published business layer. It is skipped while a hold is active,
so a known-bad day never reaches a dashboard.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sentinel.config import Settings, get_settings
from sentinel.elt import ops
from sentinel.elt.schema import COLUMN_TYPES, FOREIGN_KEYS, NOT_NULL_COLUMNS
from sentinel.elt.warehouse import Warehouse
from sentinel.generator.source import DATASETS, extract_path
from sentinel.logging import get_logger

log = get_logger(__name__)

SYSTEM_ACTOR = "pipeline"


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


@dataclass
class BronzeLoad:
    """Outcome of loading one dataset into bronze."""

    dataset: str
    arrived: bool
    rows: int
    source_file: str | None
    payload_columns: list[str] = field(default_factory=list)


@dataclass
class SilverBuild:
    """Outcome of rebuilding one silver table."""

    dataset: str
    rows: int
    rejected_rows: int
    rows_for_date: int


@dataclass
class PipelineResult:
    """Everything a caller needs to know about one pipeline run."""

    run_id: str
    business_date: date
    status: str
    started_at: datetime
    finished_at: datetime
    bronze: dict[str, BronzeLoad] = field(default_factory=dict)
    silver: dict[str, SilverBuild] = field(default_factory=dict)
    gold_published: bool = False
    blocked_by_holds: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        """Wall-clock duration of the run."""
        return (self.finished_at - self.started_at).total_seconds()


# --------------------------------------------------------------------------
# Bronze
# --------------------------------------------------------------------------


def load_bronze(
    warehouse: Warehouse,
    dataset: str,
    business_date: date,
    landing_dir: Path,
    batch_id: str,
) -> BronzeLoad:
    """Load one landing file into the bronze table, idempotently."""
    path = extract_path(landing_dir, dataset, business_date)
    table = f"bronze_{dataset}"

    warehouse.execute(f"DELETE FROM {table} WHERE _business_date = ?", [business_date])

    if not path.exists():
        log.warning("bronze.missing_partition", dataset=dataset, business_date=str(business_date))
        return BronzeLoad(dataset=dataset, arrived=False, rows=0, source_file=None)

    warehouse.execute(
        f"""
        INSERT INTO {table}
        SELECT ?, ?, ?, ?, row_number() OVER (), to_json(source)
        FROM read_csv(
            ?,
            all_varchar = true,
            header = true,
            hive_partitioning = false  -- payload must mirror the source schema exactly
        ) AS source
        """,
        [batch_id, business_date, str(path), _now(), str(path)],
    )
    rows = int(
        warehouse.scalar(f"SELECT count(*) FROM {table} WHERE _business_date = ?", [business_date])
        or 0
    )
    payload_columns = payload_keys(warehouse, dataset, business_date)
    log.info(
        "bronze.loaded",
        dataset=dataset,
        business_date=str(business_date),
        rows=rows,
        columns=len(payload_columns),
    )
    return BronzeLoad(
        dataset=dataset,
        arrived=True,
        rows=rows,
        source_file=str(path),
        payload_columns=payload_columns,
    )


def payload_keys(warehouse: Warehouse, dataset: str, business_date: date) -> list[str]:
    """Return the column names actually present in a bronze partition."""
    row = warehouse.fetch_one(
        f"""
        SELECT json_keys(payload)
        FROM bronze_{dataset}
        WHERE _business_date = ?
        LIMIT 1
        """,
        [business_date],
    )
    if row is None or row[0] is None:
        return []
    return [str(key) for key in row[0]]


# --------------------------------------------------------------------------
# Silver
# --------------------------------------------------------------------------


def _projection(warehouse: Warehouse, dataset: str) -> list[str]:
    """Build the SELECT list that projects bronze JSON into typed silver columns."""
    mapping = ops.column_mapping(warehouse, dataset)
    dataset_corrections = ops.corrections(warehouse, dataset)

    expressions: list[str] = []
    for column, sql_type in COLUMN_TYPES[dataset].items():
        candidates = mapping[column]
        casts = [
            f"TRY_CAST(json_extract_string(payload, '$.{source}') AS {sql_type})"
            for source in candidates
        ]
        expression = casts[0] if len(casts) == 1 else f"coalesce({', '.join(casts)})"

        applicable = [c for c in dataset_corrections if c.column_name == column]
        for correction in applicable:
            if correction.business_date is None:
                expression = f"({expression}) * {correction.factor}"
            else:
                expression = (
                    f"CASE WHEN _business_date = DATE '{correction.business_date.isoformat()}'"
                    f" THEN ({expression}) * {correction.factor}"
                    f" ELSE ({expression}) END"
                )
        if applicable and sql_type == "INTEGER":
            expression = f"CAST(round({expression}) AS INTEGER)"

        expressions.append(f"{expression} AS {column}")
    return expressions


def _validity_predicate(dataset: str) -> str:
    """SQL predicate that is true for rows passing structural validation."""
    clauses = [f"{column} IS NOT NULL" for column in NOT_NULL_COLUMNS[dataset]]
    for column in FOREIGN_KEYS[dataset]:
        clauses.append(f"{column} IN (SELECT sku_id FROM dim_sku)")
    return " AND ".join(clauses)


def build_silver(warehouse: Warehouse, dataset: str, business_date: date) -> SilverBuild:
    """Rebuild the silver table for a dataset from the whole bronze history."""
    spec = DATASETS[dataset]
    projection = ",\n            ".join(_projection(warehouse, dataset))
    quarantined = sorted(ops.quarantined_dates(warehouse, dataset))
    dedupe = ops.flag_enabled(warehouse, dataset, "dedupe")
    drop_invalid = ops.flag_enabled(warehouse, dataset, "quarantine_invalid_rows")

    quarantine_clause = ""
    if quarantined:
        literals = ", ".join(f"DATE '{value.isoformat()}'" for value in quarantined)
        quarantine_clause = f"WHERE _business_date NOT IN ({literals})"

    dedupe_clause = ""
    if dedupe:
        partition = ", ".join(spec.natural_key)
        dedupe_clause = (
            f"QUALIFY row_number() OVER (PARTITION BY {partition} "
            "ORDER BY _ingested_at DESC, _row_num DESC) = 1"
        )

    base_sql = f"""
        SELECT
            _business_date,
            _batch_id,
            _ingested_at,
            _row_num,
            {projection}
        FROM bronze_{dataset}
        {quarantine_clause}
    """

    warehouse.execute(
        f"""
        CREATE OR REPLACE TABLE silver_{dataset}_stage AS
        WITH extracted AS ({base_sql})
        SELECT * FROM extracted
        {dedupe_clause}
        """
    )

    # The rejected table holds exactly the rows that were *removed* from silver,
    # so the two together always reconstruct the partition without double
    # counting. With quarantine off, invalid rows stay in silver and the
    # rejected table is empty - visibly broken data rather than hidden data.
    predicate = _validity_predicate(dataset)
    reject_clause = f"WHERE NOT ({predicate})" if drop_invalid else "WHERE false"
    keep_clause = f"WHERE {predicate}" if drop_invalid else ""
    warehouse.execute(
        f"""
        CREATE OR REPLACE TABLE silver_{dataset}_rejected AS
        SELECT * FROM silver_{dataset}_stage {reject_clause}
        """
    )
    warehouse.execute(
        f"""
        CREATE OR REPLACE TABLE silver_{dataset} AS
        SELECT * FROM silver_{dataset}_stage {keep_clause}
        """
    )
    warehouse.execute(f"DROP TABLE IF EXISTS silver_{dataset}_stage")

    rows = int(warehouse.scalar(f"SELECT count(*) FROM silver_{dataset}") or 0)
    rejected = int(warehouse.scalar(f"SELECT count(*) FROM silver_{dataset}_rejected") or 0)
    rows_for_date = int(
        warehouse.scalar(
            f"SELECT count(*) FROM silver_{dataset} WHERE _business_date = ?", [business_date]
        )
        or 0
    )

    log.info(
        "silver.built",
        dataset=dataset,
        rows=rows,
        rejected=rejected,
        dedupe=dedupe,
        drop_invalid=drop_invalid,
        quarantined_partitions=len(quarantined),
    )
    return SilverBuild(
        dataset=dataset, rows=rows, rejected_rows=rejected, rows_for_date=rows_for_date
    )


# --------------------------------------------------------------------------
# Gold
# --------------------------------------------------------------------------

GOLD_SKU_DAILY_SQL = """
CREATE OR REPLACE TABLE gold_sku_daily AS
WITH sales AS (
    SELECT sale_date AS business_date, sku_id,
           sum(units_sold)    AS units_sold,
           sum(net_sales_usd) AS net_sales_usd
    FROM silver_pos_sales
    GROUP BY 1, 2
),
shipped AS (
    SELECT ship_date AS business_date, sku_id,
           sum(cases_shipped)   AS cases_shipped,
           sum(gross_value_usd) AS gross_value_usd
    FROM silver_shipments
    GROUP BY 1, 2
),
stock AS (
    SELECT snapshot_date AS business_date, sku_id,
           sum(on_hand_cases) AS on_hand_cases
    FROM silver_inventory
    GROUP BY 1, 2
),
grid AS (
    SELECT business_date, sku_id FROM sales
    UNION
    SELECT business_date, sku_id FROM shipped
    UNION
    SELECT business_date, sku_id FROM stock
)
SELECT
    grid.business_date,
    grid.sku_id,
    dim_sku.brand,
    dim_sku.category,
    coalesce(sales.units_sold, 0)        AS units_sold,
    coalesce(sales.net_sales_usd, 0.0)   AS net_sales_usd,
    coalesce(shipped.cases_shipped, 0)   AS cases_shipped,
    coalesce(shipped.gross_value_usd, 0) AS gross_value_usd,
    coalesce(stock.on_hand_cases, 0)     AS on_hand_cases
FROM grid
LEFT JOIN dim_sku USING (sku_id)
LEFT JOIN sales   ON sales.business_date   = grid.business_date AND sales.sku_id   = grid.sku_id
LEFT JOIN shipped ON shipped.business_date = grid.business_date AND shipped.sku_id = grid.sku_id
LEFT JOIN stock   ON stock.business_date   = grid.business_date AND stock.sku_id   = grid.sku_id
"""

GOLD_BRAND_DAILY_SQL = """
CREATE OR REPLACE TABLE gold_brand_daily AS
SELECT
    business_date,
    coalesce(brand, 'UNMAPPED')    AS brand,
    coalesce(category, 'UNMAPPED') AS category,
    sum(units_sold)                AS units_sold,
    round(sum(net_sales_usd), 2)   AS net_sales_usd,
    sum(cases_shipped)             AS cases_shipped,
    sum(on_hand_cases)             AS on_hand_cases,
    round(
        sum(units_sold) / nullif(sum(cases_shipped), 0), 3
    )                              AS units_per_case_shipped
FROM gold_sku_daily
GROUP BY 1, 2, 3
"""


def build_gold(warehouse: Warehouse) -> None:
    """Rebuild the published business layer."""
    warehouse.execute(GOLD_SKU_DAILY_SQL)
    warehouse.execute(GOLD_BRAND_DAILY_SQL)
    log.info("gold.built")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_pipeline(
    business_date: date,
    warehouse: Warehouse,
    settings: Settings | None = None,
    triggered_by: str = "cli",
) -> PipelineResult:
    """Run bronze, silver and gold for one business date.

    Quality checks and anomaly detection are deliberately *not* called from here:
    the pipeline produces data and metadata, the monitor consumes them. Keeping
    them separate means a failed check never leaves the warehouse half-written.
    """
    settings = settings or get_settings()
    run_id = f"RUN-{business_date.isoformat()}-{uuid.uuid4().hex[:6]}"
    started_at = _now()

    warehouse.execute(
        """
        INSERT INTO ops_pipeline_run
            (run_id, business_date, started_at, finished_at, status, triggered_by, notes)
        VALUES (?, ?, ?, NULL, 'running', ?, NULL)
        """,
        [run_id, business_date, started_at, triggered_by],
    )

    result = PipelineResult(
        run_id=run_id,
        business_date=business_date,
        status="running",
        started_at=started_at,
        finished_at=started_at,
    )

    batch_id = f"BATCH-{uuid.uuid4().hex[:8]}"
    for dataset in DATASETS:
        result.bronze[dataset] = load_bronze(
            warehouse, dataset, business_date, settings.landing_dir, batch_id
        )
    for dataset in DATASETS:
        result.silver[dataset] = build_silver(warehouse, dataset, business_date)

    holds = ops.active_holds(warehouse, business_date)
    if holds:
        result.blocked_by_holds = [hold.hold_id for hold in holds]
        result.gold_published = False
        result.status = "held"
        log.warning("gold.held", holds=result.blocked_by_holds)
    else:
        build_gold(warehouse)
        result.gold_published = True
        result.status = "succeeded"

    result.finished_at = _now()
    warehouse.execute(
        "UPDATE ops_pipeline_run SET finished_at = ?, status = ? WHERE run_id = ?",
        [result.finished_at, result.status, run_id],
    )
    log.info(
        "pipeline.finished",
        run_id=run_id,
        business_date=str(business_date),
        status=result.status,
        seconds=round(result.duration_seconds, 3),
    )
    return result
