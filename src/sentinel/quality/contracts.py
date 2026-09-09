"""Row-level data contracts.

The metric-based checks answer "is this partition healthy?". Contracts answer
"what exactly is wrong with this row?" - which is the question an engineer (or an
agent) asks second, once something has already failed. Pydantic gives typed,
quotable error messages that go straight into the incident document.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PosSalesRow(BaseModel):
    """One retailer point-of-sale line."""

    model_config = ConfigDict(extra="forbid")

    sale_date: date
    store_id: str = Field(min_length=3)
    customer_id: str = Field(min_length=3)
    sku_id: Annotated[str, Field(pattern=r"^SKU-\d{4}$")]
    units_sold: int = Field(ge=0, le=10_000)
    net_sales_usd: float = Field(ge=0)
    promo_flag: Literal["Y", "N"]


class ShipmentsRow(BaseModel):
    """One shipment line out of a distribution centre."""

    model_config = ConfigDict(extra="forbid")

    ship_date: date
    dc_id: Annotated[str, Field(pattern=r"^DC-[A-Z]{2}-\d{2}$")]
    customer_id: str = Field(min_length=3)
    sku_id: Annotated[str, Field(pattern=r"^SKU-\d{4}$")]
    cases_shipped: int = Field(ge=0, le=100_000)
    gross_value_usd: float = Field(ge=0)


class InventoryRow(BaseModel):
    """One end-of-day stock position."""

    model_config = ConfigDict(extra="forbid")

    snapshot_date: date
    dc_id: Annotated[str, Field(pattern=r"^DC-[A-Z]{2}-\d{2}$")]
    sku_id: Annotated[str, Field(pattern=r"^SKU-\d{4}$")]
    on_hand_cases: int = Field(ge=0, le=1_000_000)


CONTRACTS: dict[str, type[BaseModel]] = {
    "pos_sales": PosSalesRow,
    "shipments": ShipmentsRow,
    "inventory": InventoryRow,
}


class ContractViolation(BaseModel):
    """A single row that failed its contract."""

    row_index: int
    field: str
    error: str
    value: str | None


def validate_sample(dataset: str, rows: list[dict[str, Any]]) -> list[ContractViolation]:
    """Validate rows against a dataset contract and return readable violations.

    Only the first violation per field per row is reported, which keeps the
    output small enough to paste into an LLM prompt without truncation.
    """
    model = CONTRACTS[dataset]
    violations: list[ContractViolation] = []
    for index, row in enumerate(rows):
        try:
            model.model_validate(row)
        except ValidationError as error:
            seen: set[str] = set()
            for detail in error.errors():
                field = ".".join(str(part) for part in detail["loc"]) or "<row>"
                if field in seen:
                    continue
                seen.add(field)
                raw = row.get(field)
                violations.append(
                    ContractViolation(
                        row_index=index,
                        field=field,
                        error=str(detail["msg"]),
                        value=None if raw is None else str(raw)[:120],
                    )
                )
    return violations
