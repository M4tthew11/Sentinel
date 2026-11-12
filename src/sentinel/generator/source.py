"""Generates the daily source extracts that Sentinel ingests.

Three feeds model a consumer-goods business:

* ``pos_sales``   - retailer point-of-sale, one row per store / SKU / day;
* ``shipments``   - what left the distribution centres for each customer;
* ``inventory``   - end-of-day on-hand stock per DC and SKU.

Generation is deterministic in the business date, so re-running the demo
reproduces exactly the same history - including the injected faults.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from sentinel.config import Settings, get_settings
from sentinel.generator.catalog import (
    CUSTOMERS,
    DISTRIBUTION_CENTRES,
    SKU_BY_ID,
    SKUS,
    customer_of_store,
    store_ids,
)
from sentinel.generator.faults import FaultSpec, FaultType, apply_fault
from sentinel.logging import get_logger

log = get_logger(__name__)

#: Multiplicative demand factor per weekday (Monday = 0).
WEEKDAY_FACTOR = (0.86, 0.84, 0.92, 1.02, 1.28, 1.42, 0.78)

#: Uplift applied to a SKU that is on promotion.
PROMO_LIFT = 1.85


@dataclass(frozen=True)
class DatasetSpec:
    """Describes one source feed."""

    name: str
    date_column: str
    natural_key: tuple[str, ...]
    measures: tuple[str, ...]
    required_columns: tuple[str, ...]

    @property
    def canonical_columns(self) -> tuple[str, ...]:
        """All columns the silver layer expects to find."""
        return self.required_columns


DATASETS: dict[str, DatasetSpec] = {
    "pos_sales": DatasetSpec(
        name="pos_sales",
        date_column="sale_date",
        natural_key=("sale_date", "store_id", "sku_id"),
        measures=("units_sold", "net_sales_usd"),
        required_columns=(
            "sale_date",
            "store_id",
            "customer_id",
            "sku_id",
            "units_sold",
            "net_sales_usd",
            "promo_flag",
        ),
    ),
    "shipments": DatasetSpec(
        name="shipments",
        date_column="ship_date",
        natural_key=("ship_date", "dc_id", "customer_id", "sku_id"),
        measures=("cases_shipped", "gross_value_usd"),
        required_columns=(
            "ship_date",
            "dc_id",
            "customer_id",
            "sku_id",
            "cases_shipped",
            "gross_value_usd",
        ),
    ),
    "inventory": DatasetSpec(
        name="inventory",
        date_column="snapshot_date",
        natural_key=("snapshot_date", "dc_id", "sku_id"),
        measures=("on_hand_cases",),
        required_columns=(
            "snapshot_date",
            "dc_id",
            "sku_id",
            "on_hand_cases",
        ),
    ),
}


@dataclass(frozen=True)
class GeneratedExtract:
    """The outcome of generating one dataset for one business date."""

    dataset: str
    business_date: date
    path: Path | None
    row_count: int
    fault_type: FaultType
    """The first fault injected, or ``NONE``."""

    @property
    def arrived(self) -> bool:
        """Whether a file was actually written."""
        return self.path is not None


def _seed(business_date: date, dataset: str) -> int:
    """Stable per-date, per-dataset seed."""
    return (business_date.toordinal() * 1_000_003 + sum(ord(char) for char in dataset)) % (2**31)


def _promo_skus(business_date: date) -> set[str]:
    """SKUs on promotion during the ISO week of ``business_date``."""
    rng = np.random.default_rng(business_date.isocalendar().week * 7919)
    count = int(rng.integers(2, 5))
    chosen = rng.choice(len(SKUS), size=count, replace=False)
    return {SKUS[int(index)].sku_id for index in chosen}


def _store_assortment(store_id: str) -> list[str]:
    """The SKUs a given store carries; stable for the lifetime of the demo."""
    rng = np.random.default_rng(sum(ord(char) for char in store_id) * 104_729)
    size = int(rng.integers(11, 17))
    chosen = rng.choice(len(SKUS), size=size, replace=False)
    return [SKUS[int(index)].sku_id for index in sorted(chosen)]


def _generate_pos_sales(business_date: date) -> pd.DataFrame:
    """Build a clean point-of-sale extract."""
    rng = np.random.default_rng(_seed(business_date, "pos_sales"))
    weekday_factor = WEEKDAY_FACTOR[business_date.weekday()]
    promos = _promo_skus(business_date)

    rows: list[dict[str, object]] = []
    for store_id in store_ids():
        for sku_id in _store_assortment(store_id):
            sku = SKU_BY_ID[sku_id]
            on_promo = sku_id in promos
            mean = sku.base_daily_units * weekday_factor * (PROMO_LIFT if on_promo else 1.0)
            units = int(rng.poisson(max(mean, 0.05)))
            if units == 0:
                continue
            price = sku.list_price_usd * (0.8 if on_promo else 1.0)
            rows.append(
                {
                    "sale_date": business_date.isoformat(),
                    "store_id": store_id,
                    "customer_id": customer_of_store(store_id),
                    "sku_id": sku_id,
                    "units_sold": units,
                    "net_sales_usd": round(units * price, 2),
                    "promo_flag": "Y" if on_promo else "N",
                }
            )
    return pd.DataFrame(rows, columns=list(DATASETS["pos_sales"].required_columns))


def _generate_shipments(business_date: date) -> pd.DataFrame:
    """Build a clean shipment extract."""
    rng = np.random.default_rng(_seed(business_date, "shipments"))
    weekday_factor = WEEKDAY_FACTOR[business_date.weekday()]

    rows: list[dict[str, object]] = []
    for centre in DISTRIBUTION_CENTRES:
        for customer in CUSTOMERS:
            shipped = rng.choice(len(SKUS), size=int(rng.integers(6, 11)), replace=False)
            for index in sorted(shipped):
                sku = SKUS[int(index)]
                demand = sku.base_daily_units * customer.store_count * weekday_factor
                cases = max(1, round(rng.normal(demand / sku.units_per_case, 1.4)))
                rows.append(
                    {
                        "ship_date": business_date.isoformat(),
                        "dc_id": centre.dc_id,
                        "customer_id": customer.customer_id,
                        "sku_id": sku.sku_id,
                        "cases_shipped": cases,
                        "gross_value_usd": round(
                            cases * sku.units_per_case * sku.list_price_usd * 0.62, 2
                        ),
                    }
                )
    return pd.DataFrame(rows, columns=list(DATASETS["shipments"].required_columns))


def _generate_inventory(business_date: date) -> pd.DataFrame:
    """Build a clean inventory snapshot."""
    rng = np.random.default_rng(_seed(business_date, "inventory"))

    rows: list[dict[str, object]] = []
    for centre in DISTRIBUTION_CENTRES:
        for sku in SKUS:
            cover_days = float(rng.normal(18, 4))
            daily_cases = sku.base_daily_units * 10 / sku.units_per_case
            on_hand = max(0, round(daily_cases * max(cover_days, 1.0)))
            rows.append(
                {
                    "snapshot_date": business_date.isoformat(),
                    "dc_id": centre.dc_id,
                    "sku_id": sku.sku_id,
                    "on_hand_cases": on_hand,
                }
            )
    return pd.DataFrame(rows, columns=list(DATASETS["inventory"].required_columns))


_GENERATORS = {
    "pos_sales": _generate_pos_sales,
    "shipments": _generate_shipments,
    "inventory": _generate_inventory,
}


def extract_path(landing_dir: Path, dataset: str, business_date: date) -> Path:
    """Return the Hive-partitioned landing path for one extract."""
    return landing_dir / dataset / f"business_date={business_date.isoformat()}" / f"{dataset}.csv"


def generate_day(
    business_date: date,
    faults: Mapping[str, FaultSpec | Sequence[FaultSpec]] | None = None,
    settings: Settings | None = None,
) -> dict[str, GeneratedExtract]:
    """Generate every dataset for one business date and write it to the landing zone.

    Args:
        business_date: The date to generate.
        faults: Optional mapping of dataset name to the fault to inject, or to
            several faults applied in order (two things going wrong on the same
            partition on the same day).
        settings: Settings override, mostly for tests.

    Returns:
        Mapping of dataset name to the generation outcome.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()
    faults = faults or {}

    results: dict[str, GeneratedExtract] = {}
    for dataset, builder in _GENERATORS.items():
        frame = builder(business_date)
        specs = fault_specs(faults.get(dataset))
        fault_type = specs[0].fault_type if specs else FaultType.NONE

        maybe_frame: pd.DataFrame | None = frame
        for position, spec in enumerate(specs):
            if maybe_frame is None:
                break
            # The first fault keeps the seed it always had, so single-fault
            # scenarios corrupt exactly the same rows as before.
            rng = np.random.default_rng(_seed(business_date, dataset) + 17 + 101 * position)
            maybe_frame = apply_fault(maybe_frame, spec, rng)

        path = extract_path(settings.landing_dir, dataset, business_date)
        if maybe_frame is None:
            # "Late arrival": the file simply never lands.
            if path.exists():
                path.unlink()
            results[dataset] = GeneratedExtract(dataset, business_date, None, 0, fault_type)
            log.info("extract.withheld", dataset=dataset, business_date=str(business_date))
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        maybe_frame.to_csv(path, index=False)
        results[dataset] = GeneratedExtract(
            dataset, business_date, path, len(maybe_frame), fault_type
        )
        log.debug(
            "extract.written",
            dataset=dataset,
            business_date=str(business_date),
            rows=len(maybe_frame),
            fault=str(fault_type),
        )

    return results


def fault_specs(value: FaultSpec | Sequence[FaultSpec] | None) -> tuple[FaultSpec, ...]:
    """Normalise one fault, several faults or none into a tuple."""
    if value is None:
        return ()
    if isinstance(value, FaultSpec):
        return (value,)
    return tuple(value)


def generate_range(
    start: date,
    end: date,
    faults_by_date: Mapping[date, Mapping[str, FaultSpec | Sequence[FaultSpec]]] | None = None,
    settings: Settings | None = None,
) -> dict[date, dict[str, GeneratedExtract]]:
    """Generate an inclusive range of business dates."""
    faults_by_date = faults_by_date or {}
    results: dict[date, dict[str, GeneratedExtract]] = {}
    for offset in range((end - start).days + 1):
        business_date = date.fromordinal(start.toordinal() + offset)
        results[business_date] = generate_day(
            business_date, faults_by_date.get(business_date), settings
        )
    return results
