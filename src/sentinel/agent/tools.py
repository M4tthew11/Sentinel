"""Read-only investigation tools exposed to the diagnosis agent.

Everything here answers questions; nothing here changes state. Remediation lives
behind a separate whitelist (:mod:`sentinel.remediation.actions`) and is only
reachable by *proposing* an action in the final diagnosis, never by calling a
tool directly. Splitting "look" from "touch" means a prompt injection in the data
cannot escalate into a write - the worst it can do is make the agent read
something else.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from sentinel.elt import ops
from sentinel.elt.schema import COLUMN_TYPES
from sentinel.elt.warehouse import Warehouse
from sentinel.generator.source import DATASETS
from sentinel.logging import get_logger

log = get_logger(__name__)

MAX_SAMPLE_ROWS = 20
MAX_GROUPS = 25

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_metric_history",
        "description": (
            "Recent daily values of one pipeline metric for one dataset, oldest first. "
            "Use it to see how far today departs from normal and whether the change is "
            "a spike or a trend."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": sorted(COLUMN_TYPES)},
                "metric": {
                    "type": "string",
                    "description": (
                        "Metric name, e.g. row_count, duplicate_rate, unknown_key_rate, "
                        "null_rate__units_sold, measure_sum__units_sold, measure_avg__units_sold."
                    ),
                },
                "days": {"type": "integer", "description": "Lookback window, default 14."},
            },
            "required": ["dataset", "metric"],
        },
    },
    {
        "name": "get_partition_columns",
        "description": (
            "The column names actually present in the raw extract for a business date, "
            "next to the columns the warehouse expects and the mapping currently in use. "
            "This is how you confirm or rule out a renamed source column."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": sorted(COLUMN_TYPES)},
                "business_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["dataset"],
        },
    },
    {
        "name": "sample_rows",
        "description": (
            "A few raw rows from the extract, exactly as delivered. Set only_rejected to "
            "see rows that failed validation instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": sorted(COLUMN_TYPES)},
                "business_date": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer", "description": f"1-{MAX_SAMPLE_ROWS}, default 5."},
                "only_rejected": {"type": "boolean"},
            },
            "required": ["dataset"],
        },
    },
    {
        "name": "count_rows_by",
        "description": (
            "Row counts grouped by one raw column for the business date and for the "
            "previous business date, side by side. Use it to find out whether a volume "
            "change is spread across all groups or concentrated in a few."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": sorted(COLUMN_TYPES)},
                "group_column": {"type": "string", "description": "Raw source column name."},
                "business_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["dataset", "group_column"],
        },
    },
    {
        "name": "get_failed_checks",
        "description": "The data quality checks that failed for a dataset on a business date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": sorted(COLUMN_TYPES)},
                "business_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["dataset"],
        },
    },
]


class Toolbox:
    """Executes investigation tool calls against the warehouse."""

    def __init__(self, warehouse: Warehouse, default_date: date) -> None:
        self.warehouse = warehouse
        self.default_date = default_date
        self.call_log: list[str] = []

    # -- dispatch ----------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a named tool, returning a JSON-serialisable result."""
        self.call_log.append(name)
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "get_metric_history": self.get_metric_history,
            "get_partition_columns": self.get_partition_columns,
            "sample_rows": self.sample_rows,
            "count_rows_by": self.count_rows_by,
            "get_failed_checks": self.get_failed_checks,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            result: dict[str, Any] = handler(**arguments)
            return result
        except Exception as error:
            log.warning("tool.failed", tool=name, error=str(error))
            return {"error": f"{type(error).__name__}: {error}"}

    def _resolve_date(self, business_date: str | None) -> date:
        return self.default_date if not business_date else date.fromisoformat(business_date)

    # -- tools -------------------------------------------------------------

    def get_metric_history(self, dataset: str, metric: str, days: int = 14) -> dict[str, Any]:
        """Daily values of one metric, oldest first."""
        window = max(1, min(int(days), 120))
        rows = self.warehouse.fetch_records(
            """
            SELECT business_date, value
            FROM dq_metric
            WHERE dataset = ? AND metric = ?
            ORDER BY business_date DESC
            LIMIT ?
            """,
            [dataset, metric, window],
        )
        points = [
            {"business_date": str(row["business_date"].date()), "value": _round(row["value"])}
            for row in rows
        ]
        points.reverse()
        return {"dataset": dataset, "metric": metric, "points": points}

    def get_partition_columns(
        self, dataset: str, business_date: str | None = None
    ) -> dict[str, Any]:
        """Columns present in the extract versus the columns the warehouse expects."""
        target = self._resolve_date(business_date)
        row = self.warehouse.fetch_one(
            f"SELECT json_keys(payload) FROM bronze_{dataset} WHERE _business_date = ? LIMIT 1",
            [target],
        )
        present = [] if row is None or row[0] is None else [str(key) for key in row[0]]
        mapping = ops.column_mapping(self.warehouse, dataset)
        return {
            "dataset": dataset,
            "business_date": target.isoformat(),
            "columns_in_extract": present,
            "expected_columns": list(COLUMN_TYPES[dataset]),
            "current_mapping": mapping,
            "unresolved_columns": [
                canonical
                for canonical, candidates in mapping.items()
                if present and not any(source in present for source in candidates)
            ],
        }

    def sample_rows(
        self,
        dataset: str,
        business_date: str | None = None,
        limit: int = 5,
        only_rejected: bool = False,
    ) -> dict[str, Any]:
        """Raw rows exactly as delivered, or rows that failed validation."""
        target = self._resolve_date(business_date)
        size = max(1, min(int(limit), MAX_SAMPLE_ROWS))

        if only_rejected:
            table = f"silver_{dataset}_rejected"
            if not self.warehouse.table_exists(table):
                return {"dataset": dataset, "rows": [], "note": "no rejected rows table yet"}
            frame = self.warehouse.fetch_df(
                f"SELECT * FROM {table} WHERE _business_date = ? LIMIT ?", [target, size]
            )
            rows = json.loads(frame.to_json(orient="records", date_format="iso"))
            return {"dataset": dataset, "business_date": target.isoformat(), "rows": rows}

        frame = self.warehouse.fetch_df(
            f"SELECT payload FROM bronze_{dataset} WHERE _business_date = ? LIMIT ?",
            [target, size],
        )
        rows = [json.loads(str(value)) for value in frame["payload"].tolist()]
        return {"dataset": dataset, "business_date": target.isoformat(), "rows": rows}

    def count_rows_by(
        self,
        dataset: str,
        group_column: str,
        business_date: str | None = None,
    ) -> dict[str, Any]:
        """Group counts for the business date next to the previous business date."""
        target = self._resolve_date(business_date)
        previous = target - timedelta(days=1)
        grouped = f"""
            SELECT
                json_extract_string(payload, '$.{group_column}') AS group_value,
                count(*) FILTER (WHERE _business_date = ?)       AS rows_today,
                count(*) FILTER (WHERE _business_date = ?)       AS rows_previous
            FROM bronze_{dataset}
            WHERE _business_date IN (?, ?)
            GROUP BY 1
        """
        params = [target, previous, target, previous]

        # Distinct-group counts are computed over every group, while only a
        # sample is returned: an agent asking "which stores stopped reporting?"
        # needs the totals to be true even when the sample is truncated.
        totals = self.warehouse.fetch_one(
            f"""
            SELECT
                count(*) FILTER (WHERE rows_today > 0),
                count(*) FILTER (WHERE rows_previous > 0),
                count(*)
            FROM ({grouped})
            """,
            params,
        )
        sample = self.warehouse.fetch_records(
            f"{grouped} ORDER BY rows_previous DESC, group_value LIMIT ?",
            [*params, MAX_GROUPS],
        )
        groups = [
            {
                "value": None if row["group_value"] is None else str(row["group_value"]),
                "rows_today": int(row["rows_today"]),
                "rows_previous": int(row["rows_previous"]),
            }
            for row in sample
        ]
        return {
            "dataset": dataset,
            "group_column": group_column,
            "business_date": target.isoformat(),
            "previous_business_date": previous.isoformat(),
            "groups_today": int(totals[0]) if totals else 0,
            "groups_previous": int(totals[1]) if totals else 0,
            "groups_total": int(totals[2]) if totals else 0,
            "sample_truncated": bool(totals and int(totals[2]) > MAX_GROUPS),
            "groups": groups,
        }

    def get_failed_checks(self, dataset: str, business_date: str | None = None) -> dict[str, Any]:
        """Failed quality checks for a dataset on a business date."""
        target = self._resolve_date(business_date)
        rows = self.warehouse.fetch_records(
            """
            SELECT check_name, severity, observed, threshold, details
            FROM dq_check_result
            WHERE dataset = ? AND business_date = ? AND passed = false
            ORDER BY severity, check_name
            """,
            [dataset, target],
        )
        return {
            "dataset": dataset,
            "business_date": target.isoformat(),
            "failed_checks": [
                {
                    "check": str(row["check_name"]),
                    "severity": str(row["severity"]),
                    "observed": _round(row["observed"]),
                    "threshold": _round(row["threshold"]),
                    "details": str(row["details"]),
                }
                for row in rows
            ],
        }


def _round(value: Any) -> float | None:
    """Round a numeric value for compact tool output."""
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def dataset_overview() -> str:
    """Short description of the warehouse, used in the system prompt."""
    lines: list[str] = []
    for name, spec in DATASETS.items():
        lines.append(
            f"- {name}: natural key ({', '.join(spec.natural_key)}); "
            f"measures ({', '.join(spec.measures)}); "
            f"columns ({', '.join(spec.required_columns)})"
        )
    return "\n".join(lines)
