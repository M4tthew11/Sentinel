"""Medallion ELT: landing files -> bronze -> silver -> gold, on DuckDB."""

from sentinel.elt.pipeline import PipelineResult, run_pipeline
from sentinel.elt.warehouse import Warehouse, open_warehouse

__all__ = ["PipelineResult", "Warehouse", "open_warehouse", "run_pipeline"]
