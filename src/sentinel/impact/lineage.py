"""The lineage graph: which assets take data from which datasets.

Lineage is declared, not discovered. A YAML file lists the tables, models,
reports, dashboards and decisions built from the source datasets and what each
one reads; this module validates it and answers one question - given a dataset,
what is downstream of it, directly or through other assets.

Declaring it is a deliberate choice. The warehouse knows which tables its SQL
reads, but it cannot know that a planner opens a dashboard every morning or that
an ordering job consumes a forecast. Those are the consumers that decide what an
incident costs, and only the people who run the platform can name them.
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sentinel.incidents.models import AffectedAsset

#: The file that ships with the package and describes the demo warehouse.
DEFAULT_CONFIG_NAME = "impact.yml"


class ImpactConfigError(ValueError):
    """The lineage and cost file is missing, unreadable or inconsistent."""


class AssetKind(StrEnum):
    """What sort of thing a downstream asset is."""

    TABLE = "table"
    MODEL = "model"
    REPORT = "report"
    DASHBOARD = "dashboard"
    DECISION = "decision"


class Criticality(StrEnum):
    """How much the business leans on an asset."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AssetSpec(BaseModel):
    """One downstream asset as declared in the lineage file."""

    model_config = ConfigDict(extra="forbid")

    kind: AssetKind
    name: str = ""
    owner: str = ""
    criticality: Criticality = Criticality.MEDIUM
    depends_on: list[str] = Field(min_length=1)
    cost_per_hour: float | None = Field(default=None, ge=0)


class PriorityThresholds(BaseModel):
    """Estimated loss at or above which an incident is P1 or P2."""

    model_config = ConfigDict(extra="forbid")

    p1: float = Field(default=20000, ge=0)
    p2: float = Field(default=8000, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> PriorityThresholds:
        if self.p2 > self.p1:
            raise ValueError("priority_thresholds.p2 must not exceed p1")
        return self


class CostModel(BaseModel):
    """The rates an estimate is built from. Every one is an assumption."""

    model_config = ConfigDict(extra="forbid")

    currency: str = "USD"
    exposure_hours: float = Field(default=8, ge=0)
    cost_per_hour_by_kind: dict[AssetKind, float] = Field(
        default_factory=lambda: {
            AssetKind.TABLE: 0.0,
            AssetKind.MODEL: 60.0,
            AssetKind.REPORT: 40.0,
            AssetKind.DASHBOARD: 80.0,
            AssetKind.DECISION: 300.0,
        }
    )
    criticality_multiplier: dict[Criticality, float] = Field(
        default_factory=lambda: {
            Criticality.LOW: 0.5,
            Criticality.MEDIUM: 1.0,
            Criticality.HIGH: 2.0,
            Criticality.CRITICAL: 4.0,
        }
    )
    severity_factor: dict[str, float] = Field(
        default_factory=lambda: {"critical": 1.0, "warning": 0.5}
    )
    cost_per_consumer: float = Field(default=50, ge=0)
    cost_per_record: dict[str, float] = Field(default_factory=lambda: {"default": 0.01})
    uncertainty: float = Field(default=0.5, ge=0, le=1)
    priority_thresholds: PriorityThresholds = Field(default_factory=PriorityThresholds)

    @model_validator(mode="after")
    def _non_negative(self) -> CostModel:
        rates: dict[str, float] = {
            **{
                f"cost_per_hour_by_kind.{key}": value
                for key, value in self.cost_per_hour_by_kind.items()
            },
            **{
                f"criticality_multiplier.{key}": value
                for key, value in self.criticality_multiplier.items()
            },
            **{f"severity_factor.{key}": value for key, value in self.severity_factor.items()},
            **{f"cost_per_record.{key}": value for key, value in self.cost_per_record.items()},
        }
        negative = sorted(name for name, value in rates.items() if value < 0)
        if negative:
            raise ValueError(f"rates must not be negative: {', '.join(negative)}")
        return self

    def hourly_rate(self, spec: AssetSpec) -> float:
        """What an hour of this asset running on bad data is assumed to cost."""
        if spec.cost_per_hour is not None:
            return spec.cost_per_hour
        base = self.cost_per_hour_by_kind.get(spec.kind, 0.0)
        return base * self.criticality_multiplier.get(spec.criticality, 1.0)

    def record_rate(self, dataset: str) -> float:
        """Assumed cost of one wrong or missing record of a dataset."""
        return self.cost_per_record.get(dataset, self.cost_per_record.get("default", 0.0))


class ImpactConfig(BaseModel):
    """The whole file: sources, downstream assets and the cost model."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    sources: list[str] = Field(min_length=1)
    assets: dict[str, AssetSpec] = Field(default_factory=dict)
    cost_model: CostModel = Field(default_factory=CostModel)

    @model_validator(mode="after")
    def _consistent(self) -> ImpactConfig:
        sources = set(self.sources)
        clash = sorted(sources & set(self.assets))
        if clash:
            raise ValueError(f"declared both as a source and as an asset: {', '.join(clash)}")
        known = sources | set(self.assets)
        for asset_id, spec in self.assets.items():
            unknown = sorted(set(spec.depends_on) - known)
            if unknown:
                raise ValueError(
                    f"asset {asset_id!r} depends on {', '.join(unknown)}, "
                    "which is neither a source nor an asset"
                )
        cycle = _find_cycle(self.assets)
        if cycle:
            raise ValueError(f"lineage has a cycle: {' -> '.join(cycle)}")
        return self


class LineageGraph:
    """Downstream lookups over a validated lineage file."""

    def __init__(self, config: ImpactConfig, source: str = "") -> None:
        self.config = config
        self.source = source
        """Where the configuration was read from, for display."""

        self._readers: dict[str, list[str]] = {}
        for asset_id, spec in config.assets.items():
            for upstream in spec.depends_on:
                self._readers.setdefault(upstream, []).append(asset_id)

    @property
    def cost_model(self) -> CostModel:
        """The cost model declared alongside the lineage."""
        return self.config.cost_model

    def knows(self, dataset: str) -> bool:
        """Whether a dataset is declared as a source."""
        return dataset in self.config.sources

    def downstream(self, dataset: str) -> list[AffectedAsset]:
        """Every asset that takes data from ``dataset``, nearest first.

        Breadth-first, so each asset is reported once with its shortest path
        from the dataset. An asset reachable both directly and through another
        one is listed at the smaller distance.
        """
        paths: dict[str, list[str]] = {dataset: [dataset]}
        queue: deque[str] = deque([dataset])
        found: list[AffectedAsset] = []
        while queue:
            current = queue.popleft()
            for reader in self._readers.get(current, []):
                if reader in paths:
                    continue
                paths[reader] = [*paths[current], reader]
                queue.append(reader)
                spec = self.config.assets[reader]
                found.append(
                    AffectedAsset(
                        asset_id=reader,
                        name=spec.name or reader,
                        kind=str(spec.kind),
                        owner=spec.owner,
                        criticality=str(spec.criticality),
                        distance=len(paths[reader]) - 1,
                        via=paths[reader],
                        cost_per_hour=self.cost_model.hourly_rate(spec),
                    )
                )
        found.sort(key=lambda asset: (asset.distance, asset.asset_id))
        return found


def load_lineage(path: Path | None = None) -> LineageGraph:
    """Read and validate the lineage file.

    Args:
        path: A lineage file to use instead of the one shipped with the package.

    Raises:
        ImpactConfigError: If the file is missing, is not valid YAML, or does not
            describe a consistent graph. The message names what to fix.
    """
    if path is None:
        text = resources.files("sentinel.impact").joinpath(DEFAULT_CONFIG_NAME).read_text("utf-8")
        source = f"built-in {DEFAULT_CONFIG_NAME}"
    else:
        try:
            text = path.read_text("utf-8")
        except OSError as error:
            raise ImpactConfigError(f"cannot read lineage file {path}: {error}") from error
        source = str(path)

    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ImpactConfigError(f"{source} is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise ImpactConfigError(f"{source} must be a mapping with 'sources' and 'assets'")

    try:
        config = ImpactConfig.model_validate(raw)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or 'file'}: {issue['msg']}"
            for issue in error.errors()
        )
        raise ImpactConfigError(f"{source} is invalid: {problems}") from error
    return LineageGraph(config, source)


def _find_cycle(assets: dict[str, AssetSpec]) -> list[str]:
    """Return one dependency cycle among the assets, or an empty list."""
    done: set[str] = set()
    for start in assets:
        if start in done:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        on_path = [start]
        while stack:
            node, index = stack[-1]
            upstream = [name for name in assets[node].depends_on if name in assets]
            if index >= len(upstream):
                stack.pop()
                on_path.pop()
                done.add(node)
                continue
            stack[-1] = (node, index + 1)
            following = upstream[index]
            if following in on_path:
                return [*on_path[on_path.index(following) :], following]
            if following not in done:
                stack.append((following, 0))
                on_path.append(following)
    return []
