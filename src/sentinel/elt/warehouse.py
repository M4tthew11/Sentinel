"""Thin wrapper around the DuckDB warehouse.

DuckDB is the deliberate choice for a portfolio project: it gives real SQL,
real columnar execution and real analytical functions with zero infrastructure,
so a reviewer can run the whole platform from a clone. The SQL is written in
ANSI-ish DuckDB dialect that ports to Databricks or Synapse with small edits -
see docs/adr/0001-duckdb-as-warehouse.md.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

import duckdb
import pandas as pd

from sentinel.config import Settings, get_settings
from sentinel.elt.schema import DDL_STATEMENTS, bronze_ddl
from sentinel.generator.catalog import SKUS
from sentinel.generator.source import DATASETS
from sentinel.logging import get_logger

log = get_logger(__name__)


class Warehouse:
    """A connection to the DuckDB warehouse plus schema helpers."""

    def __init__(self, path: Path | str, read_only: bool = False) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = duckdb.connect(str(self.path), read_only=read_only)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection."""
        self._connection.close()

    def __enter__(self) -> Warehouse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- query helpers -----------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> duckdb.DuckDBPyConnection:
        """Execute a statement and return the cursor."""
        return self._connection.execute(sql, list(params) if params else None)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        """Execute a statement once per parameter row."""
        if rows:
            self._connection.executemany(sql, [list(row) for row in rows])

    def fetch_df(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        """Execute a query and return the result as a DataFrame."""
        frame: pd.DataFrame = self.execute(sql, params).fetch_df()
        return frame

    def fetch_records(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        """Execute a query and return rows as dictionaries.

        Preferred over iterating a DataFrame: callers get plain Python values
        keyed by column name, which keeps query results out of the rest of the
        codebase's type surface.
        """
        frame = self.fetch_df(sql, params)
        records: list[dict[str, Any]] = frame.to_dict(orient="records")  # type: ignore[assignment]
        return records

    def fetch_one(self, sql: str, params: Sequence[Any] | None = None) -> tuple[Any, ...] | None:
        """Execute a query and return the first row, if any."""
        row = self.execute(sql, params).fetchone()
        return tuple(row) if row is not None else None

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        """Execute a query and return the first column of the first row."""
        row = self.fetch_one(sql, params)
        return None if row is None else row[0]

    def table_exists(self, name: str) -> bool:
        """Whether a table or view with that name exists."""
        count = self.scalar(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
        )
        return bool(count)

    def columns_of(self, table: str) -> list[str]:
        """Return the column names of a table, in ordinal order."""
        frame = self.fetch_df(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = ?
            ORDER BY ordinal_position
            """,
            [table],
        )
        return [str(value) for value in frame["column_name"].tolist()]

    # -- schema ------------------------------------------------------------

    def init_schema(self) -> None:
        """Create every table the platform needs, idempotently."""
        for statement in DDL_STATEMENTS:
            self.execute(statement)
        for dataset in DATASETS:
            self.execute(bronze_ddl(dataset))
        self.seed_dimensions()
        log.debug("warehouse.schema_ready", path=str(self.path))

    def seed_dimensions(self) -> None:
        """Load the product master from the Python catalogue."""
        rows = [
            (
                sku.sku_id,
                sku.brand,
                sku.category,
                sku.pack_size,
                sku.units_per_case,
                sku.list_price_usd,
            )
            for sku in SKUS
        ]
        self.execute("DELETE FROM dim_sku")
        self.executemany("INSERT INTO dim_sku VALUES (?, ?, ?, ?, ?, ?)", rows)


@contextmanager
def open_warehouse(
    settings: Settings | None = None,
    read_only: bool = False,
    init: bool = True,
) -> Iterator[Warehouse]:
    """Open the configured warehouse as a context manager.

    Args:
        settings: Settings override, mostly for tests.
        read_only: Open without write access (used by the API).
        init: Whether to run the idempotent schema DDL first.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()
    warehouse = Warehouse(settings.warehouse_path, read_only=read_only)
    try:
        if init and not read_only:
            warehouse.init_schema()
        yield warehouse
    finally:
        warehouse.close()
