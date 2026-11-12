"""Data faults that can be injected into a generated source extract.

Each fault models a failure that actually happens in consumer-goods data feeds:
an upstream extract truncated halfway, a retailer switching from eaches to cases,
a source system renaming a column in a Friday release, a double-fired load job.

The catalogue is shared by three consumers:

* the generator, which injects the fault into a source file;
* the eval harness, which uses ``expected_root_cause`` / ``expected_actions`` as
  the ground truth for scoring the agent;
* the agent prompt, which gets the ``description`` list as background knowledge
  of what can go wrong (but never which fault is actually active).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd

UNKNOWN_SKU_PREFIX = "SKU-9"


class FaultType(StrEnum):
    """A failure mode that can be injected into a source extract."""

    NONE = "none"
    VOLUME_DROP = "volume_drop"
    NULL_SPIKE = "null_spike"
    DUPLICATE_BATCH = "duplicate_batch"
    SCHEMA_DRIFT = "schema_drift"
    UNIT_OF_MEASURE = "unit_of_measure"
    LATE_ARRIVAL = "late_arrival"
    REFERENTIAL_BREAK = "referential_break"

    # Off-catalogue faults. Nothing in the rule engine, the root-cause vocabulary
    # or the agent prompt describes them; they exist to measure what an engine
    # does with a failure nobody enumerated.
    PARTIAL_UNIT_CHANGE = "partial_unit_change"
    NEGATED_RETURNS = "negated_returns"


#: Fault types that have no root-cause label and no rule of their own.
OFF_CATALOGUE: frozenset[FaultType] = frozenset(
    {FaultType.PARTIAL_UNIT_CHANGE, FaultType.NEGATED_RETURNS}
)


@dataclass(frozen=True)
class FaultProfile:
    """Human-readable background for a fault type."""

    description: str
    upstream_story: str
    expected_actions: tuple[str, ...]
    """Remediation actions a competent on-call engineer would take."""


FAULT_LIBRARY: dict[FaultType, FaultProfile] = {
    FaultType.NONE: FaultProfile(
        description="No fault; the extract is healthy.",
        upstream_story="Normal day.",
        expected_actions=(),
    ),
    FaultType.VOLUME_DROP: FaultProfile(
        description="Row count collapses versus the historical baseline for that weekday.",
        upstream_story=(
            "The upstream extract job hit its runtime limit and wrote a partial file, "
            "so only a fraction of stores reported."
        ),
        expected_actions=("hold_downstream", "open_upstream_ticket"),
    ),
    FaultType.NULL_SPIKE: FaultProfile(
        description="A column that is normally fully populated is suddenly null on many rows.",
        upstream_story=(
            "A join in the upstream staging view started missing a lookup, so the "
            "measure or key arrives empty."
        ),
        expected_actions=("quarantine_invalid_rows", "open_upstream_ticket"),
    ),
    FaultType.DUPLICATE_BATCH: FaultProfile(
        description=(
            "The same natural keys appear more than once, inflating row count and measures."
        ),
        upstream_story="The load job was retried after a timeout and delivered the file twice.",
        expected_actions=("enable_dedupe", "rerun_pipeline"),
    ),
    FaultType.SCHEMA_DRIFT: FaultProfile(
        description=(
            "An expected column is missing from the extract; a differently named "
            "column appeared instead."
        ),
        upstream_story="The source system renamed a field in a release without notice.",
        expected_actions=("add_column_mapping", "rerun_pipeline"),
    ),
    FaultType.UNIT_OF_MEASURE: FaultProfile(
        description=(
            "Row count is normal but a measure jumps by roughly a constant factor, "
            "consistent with a unit-of-measure change."
        ),
        upstream_story="The retailer switched from reporting eaches to reporting cases.",
        expected_actions=("apply_unit_correction", "open_upstream_ticket"),
    ),
    FaultType.LATE_ARRIVAL: FaultProfile(
        description="The extract for the business date never arrived; the partition is missing.",
        upstream_story="The SFTP drop was delayed by an upstream outage.",
        expected_actions=("hold_downstream", "open_upstream_ticket"),
    ),
    FaultType.REFERENTIAL_BREAK: FaultProfile(
        description="Keys in the extract do not exist in the product master.",
        upstream_story=(
            "New items were activated in the retailer's system before the product "
            "master was synchronised."
        ),
        expected_actions=("quarantine_invalid_rows", "open_upstream_ticket"),
    ),
    FaultType.PARTIAL_UNIT_CHANGE: FaultProfile(
        description=(
            "A measure jumps on a normal row count, but only some reporting units "
            "changed unit, so there is no single factor that corrects the whole partition."
        ),
        upstream_story=(
            "Part of the retailer's estate was migrated to a new POS release that "
            "reports cases instead of eaches; the rest of the stores were not."
        ),
        expected_actions=("hold_downstream", "open_upstream_ticket"),
    ),
    FaultType.NEGATED_RETURNS: FaultProfile(
        description=(
            "A share of rows carries negative quantities for a measure that is never negative."
        ),
        upstream_story=(
            "The source started netting customer returns into the sales feed as "
            "negative quantities instead of sending them separately."
        ),
        expected_actions=("hold_downstream", "open_upstream_ticket"),
    ),
}


@dataclass(frozen=True)
class FaultSpec:
    """A single fault to inject into one dataset on one business date."""

    dataset: str
    fault_type: FaultType
    magnitude: float = 0.5
    column: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def profile(self) -> FaultProfile:
        """Return the human-readable profile of this fault type."""
        return FAULT_LIBRARY[self.fault_type]


#: Columns each fault prefers to attack, per dataset.
_DEFAULT_TARGETS: dict[str, dict[FaultType, str]] = {
    "pos_sales": {
        FaultType.NULL_SPIKE: "units_sold",
        FaultType.SCHEMA_DRIFT: "units_sold",
        FaultType.UNIT_OF_MEASURE: "units_sold",
        FaultType.PARTIAL_UNIT_CHANGE: "units_sold",
        FaultType.NEGATED_RETURNS: "units_sold",
        FaultType.REFERENTIAL_BREAK: "sku_id",
    },
    "shipments": {
        FaultType.NULL_SPIKE: "cases_shipped",
        FaultType.SCHEMA_DRIFT: "gross_value_usd",
        FaultType.UNIT_OF_MEASURE: "cases_shipped",
        FaultType.PARTIAL_UNIT_CHANGE: "cases_shipped",
        FaultType.NEGATED_RETURNS: "cases_shipped",
        FaultType.REFERENTIAL_BREAK: "sku_id",
    },
    "inventory": {
        FaultType.NULL_SPIKE: "on_hand_cases",
        FaultType.SCHEMA_DRIFT: "on_hand_cases",
        FaultType.UNIT_OF_MEASURE: "on_hand_cases",
        FaultType.PARTIAL_UNIT_CHANGE: "on_hand_cases",
        FaultType.NEGATED_RETURNS: "on_hand_cases",
        FaultType.REFERENTIAL_BREAK: "sku_id",
    },
}

#: How a drifted column is renamed by the "source system".
DRIFT_SUFFIX = "_qty"

#: The reporting unit a partial extract loses whole, per dataset.
VOLUME_GROUP_COLUMN: dict[str, str] = {
    "pos_sales": "store_id",
    "shipments": "dc_id",
    "inventory": "dc_id",
}


def target_column(spec: FaultSpec) -> str:
    """Resolve which column a fault should attack."""
    if spec.column:
        return spec.column
    dataset_targets = _DEFAULT_TARGETS.get(spec.dataset, {})
    if spec.fault_type not in dataset_targets:
        raise ValueError(f"{spec.fault_type} has no default target column for {spec.dataset}")
    return dataset_targets[spec.fault_type]


def _require_column(frame: pd.DataFrame, column: str, spec: FaultSpec) -> None:
    """Fail loudly when a fault targets a column the frame does not have.

    Without this, pandas quietly creates the column and the "fault" becomes a
    silent no-op that only shows up as an eval that mysteriously never fires.
    """
    if column not in frame.columns:
        raise ValueError(
            f"cannot inject {spec.fault_type} into {spec.dataset}: "
            f"column {column!r} is not present (have: {list(frame.columns)})"
        )


def apply_fault(
    frame: pd.DataFrame,
    spec: FaultSpec,
    rng: np.random.Generator,
) -> pd.DataFrame | None:
    """Return a corrupted copy of ``frame``.

    Args:
        frame: The clean extract.
        spec: The fault to inject.
        rng: Seeded random generator, so a business date always corrupts the same way.

    Returns:
        The corrupted frame, or ``None`` when the fault means "no file arrived".
    """
    if spec.fault_type is FaultType.NONE:
        return frame
    if spec.fault_type is FaultType.LATE_ARRIVAL:
        return None

    corrupted = frame.copy()

    if spec.fault_type is FaultType.VOLUME_DROP:
        # A truncated extract loses whole reporting units, not a random scatter
        # of rows: the job stops partway through the stores or the distribution
        # centres. Modelling it that way is what makes the shortfall traceable -
        # an investigator grouping by store sees which ones went silent.
        group_column = VOLUME_GROUP_COLUMN.get(spec.dataset)
        if group_column and group_column in corrupted.columns:
            groups = corrupted[group_column].dropna().unique()
            keep_count = max(1, round(len(groups) * (1.0 - spec.magnitude)))
            kept = set(rng.choice(groups, size=keep_count, replace=False))
            return corrupted[corrupted[group_column].isin(kept)].reset_index(drop=True)

        keep = max(1, round(len(corrupted) * (1.0 - spec.magnitude)))
        index = rng.choice(len(corrupted), size=keep, replace=False)
        return corrupted.iloc[np.sort(index)].reset_index(drop=True)

    if spec.fault_type is FaultType.NULL_SPIKE:
        column = target_column(spec)
        _require_column(corrupted, column, spec)
        count = max(1, round(len(corrupted) * spec.magnitude))
        index = rng.choice(len(corrupted), size=count, replace=False)
        corrupted.loc[corrupted.index[index], column] = None
        return corrupted

    if spec.fault_type is FaultType.DUPLICATE_BATCH:
        count = max(1, round(len(corrupted) * spec.magnitude))
        index = rng.choice(len(corrupted), size=count, replace=False)
        duplicated = corrupted.iloc[np.sort(index)]
        return pd.concat([corrupted, duplicated], ignore_index=True)

    if spec.fault_type is FaultType.SCHEMA_DRIFT:
        column = target_column(spec)
        _require_column(corrupted, column, spec)
        return corrupted.rename(columns={column: f"{column}{DRIFT_SUFFIX}"})

    if spec.fault_type is FaultType.UNIT_OF_MEASURE:
        column = target_column(spec)
        _require_column(corrupted, column, spec)
        factor = float(spec.metadata.get("factor", "12"))
        corrupted[column] = (corrupted[column].astype(float) * factor).round(3)
        return corrupted

    if spec.fault_type is FaultType.REFERENTIAL_BREAK:
        column = target_column(spec)
        _require_column(corrupted, column, spec)
        count = max(1, round(len(corrupted) * spec.magnitude))
        index = rng.choice(len(corrupted), size=count, replace=False)
        # Derive the unknown key from the row position rather than at random:
        # two randomly drawn ids can collide and turn a referential break into a
        # duplicate-key failure as well, which muddies what the fault is testing.
        unknown = [f"{UNKNOWN_SKU_PREFIX}{100 + int(position) % 900:03d}" for position in index]
        corrupted.loc[corrupted.index[index], column] = unknown
        return corrupted

    if spec.fault_type is FaultType.PARTIAL_UNIT_CHANGE:
        # Whole reporting units switch, not random rows: a POS release rolls out
        # store by store. ``magnitude`` is the share of units that switched.
        column = target_column(spec)
        _require_column(corrupted, column, spec)
        group_column = VOLUME_GROUP_COLUMN[spec.dataset]
        groups = corrupted[group_column].dropna().unique()
        switched_count = min(len(groups), max(1, round(len(groups) * spec.magnitude)))
        switched = set(rng.choice(groups, size=switched_count, replace=False))
        factor = float(spec.metadata.get("factor", "12"))
        mask = corrupted[group_column].isin(switched)
        corrupted[column] = corrupted[column].astype(float)
        corrupted.loc[mask, column] = (corrupted.loc[mask, column] * factor).round(3)
        return corrupted

    if spec.fault_type is FaultType.NEGATED_RETURNS:
        column = target_column(spec)
        _require_column(corrupted, column, spec)
        count = max(1, round(len(corrupted) * spec.magnitude))
        index = rng.choice(len(corrupted), size=count, replace=False)
        corrupted[column] = corrupted[column].astype(float)
        rows = corrupted.index[index]
        corrupted.loc[rows, column] = -corrupted.loc[rows, column].abs()
        return corrupted

    raise ValueError(f"Unhandled fault type: {spec.fault_type}")
