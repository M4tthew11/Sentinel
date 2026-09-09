"""The synthetic source system must be deterministic and faults must bite."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from sentinel.generator.faults import (
    DRIFT_SUFFIX,
    FaultSpec,
    FaultType,
    apply_fault,
)
from sentinel.generator.source import DATASETS, extract_path, generate_day


@pytest.fixture
def clean_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "sale_date": ["2025-03-01"] * 10,
            "store_id": [f"CUST-01-S{index:03d}" for index in range(10)],
            "customer_id": ["CUST-01"] * 10,
            "sku_id": ["SKU-1001"] * 10,
            "units_sold": list(range(1, 11)),
            "net_sales_usd": [11.49 * index for index in range(1, 11)],
            "promo_flag": ["N"] * 10,
        }
    )


def test_generation_is_deterministic(settings, tmp_path) -> None:
    business_date = date(2025, 3, 1)
    first = generate_day(business_date, settings=settings)
    original = extract_path(settings.landing_dir, "pos_sales", business_date).read_text()

    second = generate_day(business_date, settings=settings)
    replayed = extract_path(settings.landing_dir, "pos_sales", business_date).read_text()

    assert original == replayed
    assert first["pos_sales"].row_count == second["pos_sales"].row_count


def test_every_dataset_lands(settings) -> None:
    results = generate_day(date(2025, 3, 1), settings=settings)
    assert set(results) == set(DATASETS)
    for dataset, extract in results.items():
        assert extract.arrived
        assert extract.row_count > 0
        assert extract_path(settings.landing_dir, dataset, extract.business_date).exists()


def test_late_arrival_withholds_the_file(settings) -> None:
    business_date = date(2025, 3, 1)
    generate_day(business_date, settings=settings)
    results = generate_day(
        business_date,
        {"shipments": FaultSpec("shipments", FaultType.LATE_ARRIVAL)},
        settings,
    )
    assert not results["shipments"].arrived
    assert not extract_path(settings.landing_dir, "shipments", business_date).exists()


def test_volume_drop_removes_rows(clean_frame) -> None:
    spec = FaultSpec("pos_sales", FaultType.VOLUME_DROP, magnitude=0.6)
    corrupted = apply_fault(clean_frame, spec, np.random.default_rng(0))
    assert corrupted is not None
    assert len(corrupted) == 4


def test_duplicate_batch_adds_rows(clean_frame) -> None:
    spec = FaultSpec("pos_sales", FaultType.DUPLICATE_BATCH, magnitude=0.5)
    corrupted = apply_fault(clean_frame, spec, np.random.default_rng(0))
    assert corrupted is not None
    assert len(corrupted) == 15
    assert corrupted.duplicated().any()


def test_null_spike_nulls_the_target_column(clean_frame) -> None:
    spec = FaultSpec("pos_sales", FaultType.NULL_SPIKE, magnitude=0.4)
    corrupted = apply_fault(clean_frame, spec, np.random.default_rng(0))
    assert corrupted is not None
    assert corrupted["units_sold"].isna().sum() == 4


def test_schema_drift_renames_a_column(clean_frame) -> None:
    spec = FaultSpec("pos_sales", FaultType.SCHEMA_DRIFT)
    corrupted = apply_fault(clean_frame, spec, np.random.default_rng(0))
    assert corrupted is not None
    assert "units_sold" not in corrupted.columns
    assert f"units_sold{DRIFT_SUFFIX}" in corrupted.columns


def test_unit_change_scales_the_measure(clean_frame) -> None:
    spec = FaultSpec(
        "pos_sales", FaultType.UNIT_OF_MEASURE, column="units_sold", metadata={"factor": "12"}
    )
    corrupted = apply_fault(clean_frame, spec, np.random.default_rng(0))
    assert corrupted is not None
    assert corrupted["units_sold"].sum() == pytest.approx(clean_frame["units_sold"].sum() * 12)


def test_referential_break_introduces_unknown_keys(clean_frame) -> None:
    spec = FaultSpec("pos_sales", FaultType.REFERENTIAL_BREAK, magnitude=0.3)
    corrupted = apply_fault(clean_frame, spec, np.random.default_rng(0))
    assert corrupted is not None
    unknown = [value for value in corrupted["sku_id"] if value.startswith("SKU-9")]
    assert len(unknown) == 3


def test_fault_targeting_a_missing_column_fails_loudly() -> None:
    """A fault that silently no-ops would corrupt every eval built on it."""
    spec = FaultSpec("pos_sales", FaultType.NULL_SPIKE)
    frame = pd.DataFrame({"other": [1]})
    with pytest.raises(ValueError, match="units_sold"):
        apply_fault(frame, spec, np.random.default_rng(0))
