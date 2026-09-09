"""Static master data for the simulated CPG business.

Brands here are fictional. They mirror the shape of a real consumer-goods
portfolio (a handful of categories, a few SKUs per brand, different price points
and velocities) without using anyone's trademarks.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Sku:
    """A sellable unit."""

    sku_id: str
    brand: str
    category: str
    pack_size: str
    units_per_case: int
    list_price_usd: float
    base_daily_units: float
    """Average units sold per store per day, before seasonality and promotions."""


@dataclass(frozen=True)
class Customer:
    """A retail customer (the account Sentinel ships to)."""

    customer_id: str
    name: str
    store_count: int


@dataclass(frozen=True)
class DistributionCentre:
    """A distribution centre shipping to customers."""

    dc_id: str
    name: str
    region: str


SKUS: tuple[Sku, ...] = (
    # Fabric Care
    Sku("SKU-1001", "Aurora", "Fabric Care", "1.2L", 12, 11.49, 6.2),
    Sku("SKU-1002", "Aurora", "Fabric Care", "2.4L", 8, 19.99, 3.1),
    Sku("SKU-1003", "Aurora", "Fabric Care", "48ct pods", 6, 22.49, 2.4),
    Sku("SKU-1004", "Aurora", "Fabric Care", "0.6L travel", 24, 5.99, 1.6),
    # Baby Care
    Sku("SKU-2001", "Nimbus", "Baby Care", "size 3 / 62ct", 4, 27.99, 4.8),
    Sku("SKU-2002", "Nimbus", "Baby Care", "size 4 / 54ct", 4, 27.99, 4.4),
    Sku("SKU-2003", "Nimbus", "Baby Care", "wipes 8x64", 6, 18.49, 5.9),
    Sku("SKU-2004", "Nimbus", "Baby Care", "size 5 / 48ct", 4, 29.49, 3.0),
    # Hair Care
    Sku("SKU-3001", "Lumen", "Hair Care", "400ml shampoo", 12, 8.99, 7.5),
    Sku("SKU-3002", "Lumen", "Hair Care", "400ml conditioner", 12, 8.99, 6.1),
    Sku("SKU-3003", "Lumen", "Hair Care", "750ml shampoo", 8, 13.49, 2.8),
    Sku("SKU-3004", "Lumen", "Hair Care", "dry shampoo 200ml", 12, 9.79, 2.2),
    # Oral Care
    Sku("SKU-4001", "Veritas", "Oral Care", "toothpaste 100ml", 24, 4.49, 9.3),
    Sku("SKU-4002", "Veritas", "Oral Care", "toothpaste 2x100ml", 12, 7.99, 5.2),
    Sku("SKU-4003", "Veritas", "Oral Care", "mouthwash 500ml", 12, 6.49, 3.7),
    Sku("SKU-4004", "Veritas", "Oral Care", "floss 50m", 36, 3.29, 2.9),
    # Home Care
    Sku("SKU-5001", "Solara", "Home Care", "surface spray 750ml", 12, 5.49, 6.8),
    Sku("SKU-5002", "Solara", "Home Care", "dish liquid 900ml", 12, 4.99, 8.1),
    Sku("SKU-5003", "Solara", "Home Care", "dish liquid 450ml", 24, 3.29, 4.5),
    Sku("SKU-5004", "Solara", "Home Care", "air care refill", 18, 7.29, 2.0),
)

CUSTOMERS: tuple[Customer, ...] = (
    Customer("CUST-01", "Meridian Markets", 14),
    Customer("CUST-02", "Northline Grocers", 11),
    Customer("CUST-03", "ValuMart", 9),
    Customer("CUST-04", "CityFresh Convenience", 6),
)

DISTRIBUTION_CENTRES: tuple[DistributionCentre, ...] = (
    DistributionCentre("DC-EU-01", "Warsaw DC", "EU-Central"),
    DistributionCentre("DC-EU-02", "Rotterdam DC", "EU-West"),
    DistributionCentre("DC-EU-03", "Milan DC", "EU-South"),
)

SKU_BY_ID: dict[str, Sku] = {sku.sku_id: sku for sku in SKUS}


def store_ids() -> list[str]:
    """Return every store id, in a stable order."""
    return [
        f"{customer.customer_id}-S{index:03d}"
        for customer in CUSTOMERS
        for index in range(1, customer.store_count + 1)
    ]


def customer_of_store(store_id: str) -> str:
    """Extract the owning customer id from a store id."""
    return store_id.rsplit("-S", maxsplit=1)[0]
