"""Operational state of the pipeline.

Everything in this module is *configuration the pipeline reads at run time*:
column mappings, feature flags, quarantined partitions, downstream holds,
value corrections. It is also the only state the remediation agent is allowed
to change, which is what turns "the agent suggested a fix" into "the pipeline
behaves differently on the next run".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sentinel.elt.schema import COLUMN_TYPES
from sentinel.elt.warehouse import Warehouse


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Column mappings
# --------------------------------------------------------------------------


def column_mapping(warehouse: Warehouse, dataset: str) -> dict[str, list[str]]:
    """Return canonical column -> candidate source columns, in resolution order.

    The canonical name itself always comes first, so a mapping added to cope with
    a renamed field never breaks the partitions that still use the old name: the
    projection simply falls through to the next candidate.
    """
    mapping = {column: [column] for column in COLUMN_TYPES[dataset]}
    rows = warehouse.fetch_records(
        """
        SELECT canonical_column, source_column
        FROM ops_column_mapping
        WHERE dataset = ?
        ORDER BY created_at, source_column
        """,
        [dataset],
    )
    for row in rows:
        canonical, source = str(row["canonical_column"]), str(row["source_column"])
        candidates = mapping.setdefault(canonical, [])
        if source not in candidates:
            candidates.append(source)
    return mapping


def set_column_mapping(
    warehouse: Warehouse,
    dataset: str,
    canonical_column: str,
    source_column: str,
    actor: str,
) -> None:
    """Let a canonical column also resolve from a differently named source column."""
    if canonical_column not in COLUMN_TYPES[dataset]:
        raise ValueError(f"{canonical_column!r} is not a canonical column of {dataset!r}")
    warehouse.execute(
        """
        INSERT OR REPLACE INTO ops_column_mapping
            (dataset, canonical_column, source_column, created_at, created_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        [dataset, canonical_column, source_column, _now(), actor],
    )


# --------------------------------------------------------------------------
# Feature flags
# --------------------------------------------------------------------------


def set_flag(warehouse: Warehouse, scope: str, name: str, value: str, actor: str) -> None:
    """Set an operational flag, e.g. ``dedupe`` for a dataset."""
    warehouse.execute(
        """
        INSERT OR REPLACE INTO ops_flag (scope, name, value, updated_at, updated_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        [scope, name, value, _now(), actor],
    )


def get_flag(warehouse: Warehouse, scope: str, name: str) -> str | None:
    """Read an operational flag."""
    value = warehouse.scalar(
        "SELECT value FROM ops_flag WHERE scope = ? AND name = ?", [scope, name]
    )
    return None if value is None else str(value)


def flag_enabled(warehouse: Warehouse, scope: str, name: str) -> bool:
    """Whether a boolean flag is on."""
    return (get_flag(warehouse, scope, name) or "").lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------
# Quarantine
# --------------------------------------------------------------------------


def quarantine_partition(
    warehouse: Warehouse,
    dataset: str,
    business_date: date,
    reason: str,
    actor: str,
) -> None:
    """Exclude one partition of one dataset from the silver layer."""
    warehouse.execute(
        """
        INSERT OR REPLACE INTO ops_quarantine
            (dataset, business_date, reason, created_at, created_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        [dataset, business_date, reason, _now(), actor],
    )


def quarantined_dates(warehouse: Warehouse, dataset: str) -> set[date]:
    """Return the quarantined business dates for a dataset."""
    frame = warehouse.fetch_df(
        "SELECT business_date FROM ops_quarantine WHERE dataset = ?", [dataset]
    )
    return {value.date() for value in frame["business_date"].tolist()}


# --------------------------------------------------------------------------
# Holds
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Hold:
    """A publication hold on one business date."""

    hold_id: str
    dataset: str | None
    business_date: date
    reason: str
    created_at: datetime


def create_hold(
    warehouse: Warehouse,
    dataset: str | None,
    business_date: date,
    reason: str,
    actor: str,
) -> str:
    """Block gold publication for one business date until a human releases it."""
    hold_id = f"HOLD-{uuid.uuid4().hex[:8]}"
    warehouse.execute(
        """
        INSERT INTO ops_hold
            (hold_id, dataset, business_date, reason, created_at, created_by, released_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        """,
        [hold_id, dataset, business_date, reason, _now(), actor],
    )
    return hold_id


def active_holds(warehouse: Warehouse, business_date: date | None = None) -> list[Hold]:
    """Return unreleased holds, optionally for one business date."""
    sql = """
        SELECT hold_id, dataset, business_date, reason, created_at
        FROM ops_hold
        WHERE released_at IS NULL
    """
    params: list[object] = []
    if business_date is not None:
        sql += " AND business_date = ?"
        params.append(business_date)
    return [
        Hold(
            hold_id=str(row["hold_id"]),
            dataset=None if row["dataset"] is None else str(row["dataset"]),
            business_date=row["business_date"].date(),
            reason=str(row["reason"]),
            created_at=row["created_at"].to_pydatetime(),
        )
        for row in warehouse.fetch_records(sql + " ORDER BY created_at", params)
    ]


def release_hold(warehouse: Warehouse, hold_id: str) -> bool:
    """Release a hold; returns whether it was active."""
    was_active = warehouse.scalar(
        "SELECT count(*) FROM ops_hold WHERE hold_id = ? AND released_at IS NULL", [hold_id]
    )
    warehouse.execute("UPDATE ops_hold SET released_at = ? WHERE hold_id = ?", [_now(), hold_id])
    return bool(was_active)


# --------------------------------------------------------------------------
# Tickets and corrections
# --------------------------------------------------------------------------


def create_ticket(
    warehouse: Warehouse,
    dataset: str | None,
    summary: str,
    body: str,
    actor: str,
) -> str:
    """Record a ticket for the upstream data owner.

    In a production deployment this would open a GitHub issue or a ServiceNow
    ticket; here it is a row, so the demo has no external dependency.
    """
    ticket_id = f"TICKET-{uuid.uuid4().hex[:8]}"
    warehouse.execute(
        """
        INSERT INTO ops_ticket (ticket_id, dataset, summary, body, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [ticket_id, dataset, summary, body, _now(), actor],
    )
    return ticket_id


def add_correction(
    warehouse: Warehouse,
    dataset: str,
    column_name: str,
    factor: float,
    business_date: date | None,
    actor: str,
) -> str:
    """Register a multiplicative correction applied to a measure in silver."""
    if column_name not in COLUMN_TYPES[dataset]:
        raise ValueError(f"{column_name!r} is not a column of {dataset!r}")
    if factor <= 0:
        raise ValueError("correction factor must be positive")
    correction_id = f"CORR-{uuid.uuid4().hex[:8]}"
    warehouse.execute(
        """
        INSERT INTO ops_correction
            (correction_id, dataset, column_name, factor, business_date, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [correction_id, dataset, column_name, factor, business_date, _now(), actor],
    )
    return correction_id


@dataclass(frozen=True)
class Correction:
    """A registered measure correction."""

    dataset: str
    column_name: str
    factor: float
    business_date: date | None


def corrections(warehouse: Warehouse, dataset: str) -> list[Correction]:
    """Return the corrections registered for a dataset."""
    rows = warehouse.fetch_records(
        """
        SELECT dataset, column_name, factor, business_date
        FROM ops_correction
        WHERE dataset = ?
        ORDER BY created_at
        """,
        [dataset],
    )
    return [
        Correction(
            dataset=str(row["dataset"]),
            column_name=str(row["column_name"]),
            factor=float(row["factor"]),
            business_date=None if row["business_date"] is None else row["business_date"].date(),
        )
        for row in rows
    ]
