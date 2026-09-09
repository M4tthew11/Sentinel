"""Synthetic CPG source-system simulator with injectable data faults."""

from sentinel.generator.catalog import CUSTOMERS, DISTRIBUTION_CENTRES, SKUS, Sku
from sentinel.generator.faults import FaultSpec, FaultType
from sentinel.generator.source import DATASETS, generate_day, generate_range

__all__ = [
    "CUSTOMERS",
    "DATASETS",
    "DISTRIBUTION_CENTRES",
    "SKUS",
    "FaultSpec",
    "FaultType",
    "Sku",
    "generate_day",
    "generate_range",
]
