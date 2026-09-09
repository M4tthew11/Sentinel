"""The medallion pipeline: lossless bronze, rebuildable silver, guarded gold."""

from __future__ import annotations

import json
from datetime import date

from sentinel.elt import ops
from sentinel.elt.pipeline import run_pipeline
from sentinel.generator.faults import FaultSpec, FaultType
from sentinel.generator.source import DATASETS


def test_bronze_keeps_the_source_schema_verbatim(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date)
    run_pipeline(business_date, warehouse, settings)

    payload = warehouse.scalar("SELECT payload FROM bronze_pos_sales LIMIT 1")
    keys = set(json.loads(str(payload)))
    assert keys == set(DATASETS["pos_sales"].required_columns)


def test_bronze_load_is_idempotent(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date)
    first = run_pipeline(business_date, warehouse, settings)
    second = run_pipeline(business_date, warehouse, settings)
    assert first.bronze["pos_sales"].rows == second.bronze["pos_sales"].rows
    assert first.silver["pos_sales"].rows == second.silver["pos_sales"].rows


def test_missing_partition_is_recorded_not_fatal(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("shipments", FaultType.LATE_ARRIVAL))
    result = run_pipeline(business_date, warehouse, settings)
    assert result.bronze["shipments"].arrived is False
    assert result.bronze["shipments"].rows == 0
    assert result.bronze["pos_sales"].rows > 0


def test_gold_joins_the_three_feeds(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date)
    run_pipeline(business_date, warehouse, settings)

    brands = warehouse.fetch_df("SELECT * FROM gold_brand_daily")
    assert not brands.empty
    assert set(brands["brand"]) >= {"Aurora", "Nimbus", "Lumen", "Veritas", "Solara"}
    assert (brands["units_sold"] > 0).all()


def test_dedupe_flag_collapses_duplicate_keys(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("shipments", FaultType.DUPLICATE_BATCH, magnitude=0.5))
    before = run_pipeline(business_date, warehouse, settings)

    ops.set_flag(warehouse, "shipments", "dedupe", "true", "test")
    after = run_pipeline(business_date, warehouse, settings)

    assert after.silver["shipments"].rows < before.silver["shipments"].rows
    duplicates = warehouse.scalar(
        """
        SELECT count(*) FROM (
            SELECT ship_date, dc_id, customer_id, sku_id
            FROM silver_shipments
            GROUP BY ALL HAVING count(*) > 1
        )
        """
    )
    assert duplicates == 0


def test_column_mapping_repairs_drift_without_breaking_history(settings, warehouse, inject) -> None:
    clean_day = date(2025, 3, 1)
    drift_day = date(2025, 3, 2)
    inject(clean_day)
    inject(drift_day, FaultSpec("inventory", FaultType.SCHEMA_DRIFT))
    run_pipeline(clean_day, warehouse, settings)
    run_pipeline(drift_day, warehouse, settings)

    nulls = warehouse.scalar(
        "SELECT count(*) FROM silver_inventory WHERE _business_date = ? AND on_hand_cases IS NULL",
        [drift_day],
    )
    assert nulls > 0

    ops.set_column_mapping(warehouse, "inventory", "on_hand_cases", "on_hand_cases_qty", "test")
    run_pipeline(drift_day, warehouse, settings)

    remaining = warehouse.scalar(
        "SELECT count(*) FROM silver_inventory WHERE on_hand_cases IS NULL"
    )
    assert remaining == 0, "the mapping must resolve the drifted day"

    historical = warehouse.scalar(
        "SELECT count(*) FROM silver_inventory WHERE _business_date = ?", [clean_day]
    )
    assert historical > 0, "the mapping must not break partitions using the old name"


def test_quarantine_moves_invalid_rows_out_of_silver(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("pos_sales", FaultType.REFERENTIAL_BREAK, magnitude=0.25))
    run_pipeline(business_date, warehouse, settings)

    assert warehouse.scalar("SELECT count(*) FROM silver_pos_sales_rejected") == 0

    ops.set_flag(warehouse, "pos_sales", "quarantine_invalid_rows", "true", "test")
    run_pipeline(business_date, warehouse, settings)

    rejected = warehouse.scalar("SELECT count(*) FROM silver_pos_sales_rejected")
    orphans = warehouse.scalar(
        "SELECT count(*) FROM silver_pos_sales WHERE sku_id NOT IN (SELECT sku_id FROM dim_sku)"
    )
    assert rejected > 0
    assert orphans == 0


def test_hold_blocks_only_its_own_business_date(settings, warehouse, inject) -> None:
    held_day = date(2025, 3, 1)
    other_day = date(2025, 3, 2)
    inject(held_day)
    inject(other_day)

    ops.create_hold(warehouse, "pos_sales", held_day, "partial delivery", "test")

    assert run_pipeline(held_day, warehouse, settings).gold_published is False
    assert run_pipeline(other_day, warehouse, settings).gold_published is True


def test_unit_correction_applies_to_one_date_only(settings, warehouse, inject) -> None:
    first = date(2025, 3, 1)
    second = date(2025, 3, 2)
    inject(first)
    inject(second)
    run_pipeline(first, warehouse, settings)
    run_pipeline(second, warehouse, settings)

    baseline = warehouse.scalar(
        "SELECT sum(units_sold) FROM silver_pos_sales WHERE _business_date = ?", [second]
    )
    ops.add_correction(warehouse, "pos_sales", "units_sold", 0.5, first, "test")
    run_pipeline(first, warehouse, settings)

    unchanged = warehouse.scalar(
        "SELECT sum(units_sold) FROM silver_pos_sales WHERE _business_date = ?", [second]
    )
    assert unchanged == baseline
