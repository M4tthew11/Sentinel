"""Physical schema of the warehouse.

The silver layer is generated from these declarations rather than hand-written
DDL, which is what makes the ``add_column_mapping`` remediation possible: a
renamed source column only changes how the payload is projected, never the
contract downstream models depend on.
"""

from __future__ import annotations

#: Canonical silver column -> DuckDB type, per dataset.
COLUMN_TYPES: dict[str, dict[str, str]] = {
    "pos_sales": {
        "sale_date": "DATE",
        "store_id": "VARCHAR",
        "customer_id": "VARCHAR",
        "sku_id": "VARCHAR",
        "units_sold": "INTEGER",
        "net_sales_usd": "DOUBLE",
        "promo_flag": "VARCHAR",
    },
    "shipments": {
        "ship_date": "DATE",
        "dc_id": "VARCHAR",
        "customer_id": "VARCHAR",
        "sku_id": "VARCHAR",
        "cases_shipped": "INTEGER",
        "gross_value_usd": "DOUBLE",
    },
    "inventory": {
        "snapshot_date": "DATE",
        "dc_id": "VARCHAR",
        "sku_id": "VARCHAR",
        "on_hand_cases": "INTEGER",
    },
}

#: Columns that must never be null in silver.
NOT_NULL_COLUMNS: dict[str, tuple[str, ...]] = {
    "pos_sales": ("sale_date", "store_id", "sku_id", "units_sold"),
    "shipments": ("ship_date", "dc_id", "customer_id", "sku_id", "cases_shipped"),
    "inventory": ("snapshot_date", "dc_id", "sku_id", "on_hand_cases"),
}

#: Columns validated against the product master.
FOREIGN_KEYS: dict[str, tuple[str, ...]] = {
    "pos_sales": ("sku_id",),
    "shipments": ("sku_id",),
    "inventory": ("sku_id",),
}

DDL_STATEMENTS: tuple[str, ...] = (
    # ---- run metadata -----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS ops_pipeline_run (
        run_id        VARCHAR PRIMARY KEY,
        business_date DATE      NOT NULL,
        started_at    TIMESTAMP NOT NULL,
        finished_at   TIMESTAMP,
        status        VARCHAR   NOT NULL,
        triggered_by  VARCHAR   NOT NULL,
        notes         VARCHAR
    )
    """,
    # ---- data quality -----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS dq_metric (
        run_id        VARCHAR   NOT NULL,
        business_date DATE      NOT NULL,
        dataset       VARCHAR   NOT NULL,
        metric        VARCHAR   NOT NULL,
        value         DOUBLE,
        computed_at   TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dq_check_result (
        run_id        VARCHAR   NOT NULL,
        business_date DATE      NOT NULL,
        dataset       VARCHAR   NOT NULL,
        check_name    VARCHAR   NOT NULL,
        severity      VARCHAR   NOT NULL,
        passed        BOOLEAN   NOT NULL,
        observed      DOUBLE,
        threshold     DOUBLE,
        details       VARCHAR,
        evaluated_at  TIMESTAMP NOT NULL
    )
    """,
    # ---- operational state the agent is allowed to change -----------------
    """
    -- Mappings are additive: a canonical column may resolve from several source
    -- names, tried in order. That is what lets a rename be fixed forward without
    -- breaking the partitions that used the old name.
    CREATE TABLE IF NOT EXISTS ops_column_mapping (
        dataset          VARCHAR   NOT NULL,
        canonical_column VARCHAR   NOT NULL,
        source_column    VARCHAR   NOT NULL,
        created_at       TIMESTAMP NOT NULL,
        created_by       VARCHAR   NOT NULL,
        PRIMARY KEY (dataset, canonical_column, source_column)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ops_flag (
        scope      VARCHAR   NOT NULL,
        name       VARCHAR   NOT NULL,
        value      VARCHAR   NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        updated_by VARCHAR   NOT NULL,
        PRIMARY KEY (scope, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ops_quarantine (
        dataset       VARCHAR   NOT NULL,
        business_date DATE      NOT NULL,
        reason        VARCHAR,
        created_at    TIMESTAMP NOT NULL,
        created_by    VARCHAR   NOT NULL,
        PRIMARY KEY (dataset, business_date)
    )
    """,
    """
    -- A hold blocks publication of one business date, not the whole warehouse:
    -- a bad Tuesday must not stop Wednesday's numbers from reaching the business.
    CREATE TABLE IF NOT EXISTS ops_hold (
        hold_id       VARCHAR PRIMARY KEY,
        dataset       VARCHAR,
        business_date DATE      NOT NULL,
        reason        VARCHAR   NOT NULL,
        created_at    TIMESTAMP NOT NULL,
        created_by    VARCHAR   NOT NULL,
        released_at   TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ops_ticket (
        ticket_id  VARCHAR PRIMARY KEY,
        dataset    VARCHAR,
        summary    VARCHAR   NOT NULL,
        body       VARCHAR,
        created_at TIMESTAMP NOT NULL,
        created_by VARCHAR   NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ops_correction (
        correction_id VARCHAR PRIMARY KEY,
        dataset       VARCHAR   NOT NULL,
        column_name   VARCHAR   NOT NULL,
        factor        DOUBLE    NOT NULL,
        business_date DATE,
        created_at    TIMESTAMP NOT NULL,
        created_by    VARCHAR   NOT NULL
    )
    """,
    # ---- incidents --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS ops_incident (
        incident_id   VARCHAR PRIMARY KEY,
        run_id        VARCHAR   NOT NULL,
        business_date DATE      NOT NULL,
        dataset       VARCHAR   NOT NULL,
        severity      VARCHAR   NOT NULL,
        status        VARCHAR   NOT NULL,
        detected_at   TIMESTAMP NOT NULL,
        updated_at    TIMESTAMP NOT NULL,
        document      JSON      NOT NULL
    )
    """,
    # ---- dimensions -------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS dim_sku (
        sku_id         VARCHAR PRIMARY KEY,
        brand          VARCHAR NOT NULL,
        category       VARCHAR NOT NULL,
        pack_size      VARCHAR NOT NULL,
        units_per_case INTEGER NOT NULL,
        list_price_usd DOUBLE  NOT NULL
    )
    """,
)


def bronze_ddl(dataset: str) -> str:
    """Return the bronze table DDL for one dataset.

    Bronze keeps the payload as JSON on purpose: an extract whose schema drifted
    still lands losslessly, and the schema change becomes something the pipeline
    can observe instead of something that kills the load.
    """
    return f"""
    CREATE TABLE IF NOT EXISTS bronze_{dataset} (
        _batch_id      VARCHAR   NOT NULL,
        _business_date DATE      NOT NULL,
        _source_file   VARCHAR   NOT NULL,
        _ingested_at   TIMESTAMP NOT NULL,
        _row_num       BIGINT    NOT NULL,
        payload        JSON      NOT NULL
    )
    """
