"""The demo scenario: a clean history followed by one fault per day.

Used by three consumers, which is the point - the demo a reviewer runs, the eval
harness that scores the agent, and the integration tests all exercise the same
incidents, so a passing eval says something about the demo rather than about a
fixture written to make the eval pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sentinel.generator.faults import FaultSpec, FaultType
from sentinel.incidents.models import RootCause

#: Enough clean history for the detector to learn a weekday baseline.
HISTORY_DAYS = 42


@dataclass(frozen=True)
class ScenarioDay:
    """One business date, the faults injected into it, and the expected verdict."""

    business_date: date
    faults: dict[str, FaultSpec] = field(default_factory=dict)
    dataset: str | None = None
    expected_root_cause: RootCause | None = None
    expected_actions: tuple[str, ...] = ()
    note: str = ""

    @property
    def is_incident(self) -> bool:
        """Whether this day is expected to open an incident."""
        return self.expected_root_cause is not None


@dataclass(frozen=True)
class Scenario:
    """A full demo timeline."""

    history_start: date
    history_end: date
    days: tuple[ScenarioDay, ...]

    @property
    def start(self) -> date:
        """First date in the scenario."""
        return self.history_start

    @property
    def end(self) -> date:
        """Last date in the scenario."""
        return self.days[-1].business_date if self.days else self.history_end

    @property
    def history_dates(self) -> list[date]:
        """The clean warm-up dates."""
        span = (self.history_end - self.history_start).days + 1
        return [self.history_start + timedelta(days=offset) for offset in range(span)]

    @property
    def incident_days(self) -> tuple[ScenarioDay, ...]:
        """Only the days that should produce an incident."""
        return tuple(day for day in self.days if day.is_incident)

    def faults_by_date(self) -> dict[date, dict[str, FaultSpec]]:
        """Fault specs keyed by business date, for the generator."""
        return {day.business_date: day.faults for day in self.days if day.faults}


def build_scenario(history_start: date = date(2025, 1, 6)) -> Scenario:
    """Build the standard demo scenario.

    The schema drift is deliberately persistent: once the source system renames a
    field it keeps the new name, so the day after the fix is a clean day only if
    the remediation actually worked. That is the difference between a fix and a
    coincidence.
    """
    history_end = history_start + timedelta(days=HISTORY_DAYS - 1)
    first = history_end + timedelta(days=1)

    def day(offset: int) -> date:
        return first + timedelta(days=offset)

    drift = FaultSpec("inventory", FaultType.SCHEMA_DRIFT)

    days = (
        ScenarioDay(
            business_date=day(0),
            faults={"pos_sales": FaultSpec("pos_sales", FaultType.VOLUME_DROP, magnitude=0.65)},
            dataset="pos_sales",
            expected_root_cause=RootCause.VOLUME_DROP,
            expected_actions=("hold_downstream", "open_upstream_ticket"),
            note="Upstream extract truncated: only a third of stores reported.",
        ),
        ScenarioDay(
            business_date=day(1),
            faults={"shipments": FaultSpec("shipments", FaultType.DUPLICATE_BATCH, magnitude=0.5)},
            dataset="shipments",
            expected_root_cause=RootCause.DUPLICATE_BATCH,
            expected_actions=("enable_dedupe",),
            note="Load job retried after a timeout and delivered the file twice.",
        ),
        ScenarioDay(
            business_date=day(2),
            faults={
                "pos_sales": FaultSpec(
                    "pos_sales",
                    FaultType.UNIT_OF_MEASURE,
                    column="units_sold",
                    metadata={"factor": "12"},
                )
            },
            dataset="pos_sales",
            expected_root_cause=RootCause.UNIT_OF_MEASURE,
            expected_actions=("apply_unit_correction",),
            note="Retailer switched from reporting eaches to cases.",
        ),
        ScenarioDay(
            business_date=day(3),
            faults={"inventory": FaultSpec("inventory", FaultType.NULL_SPIKE, magnitude=0.35)},
            dataset="inventory",
            expected_root_cause=RootCause.NULL_SPIKE,
            expected_actions=("quarantine_invalid_rows",),
            note="Upstream staging view lost a lookup; on-hand quantities arrive empty.",
        ),
        ScenarioDay(
            business_date=day(4),
            faults={"shipments": FaultSpec("shipments", FaultType.LATE_ARRIVAL)},
            dataset="shipments",
            expected_root_cause=RootCause.LATE_ARRIVAL,
            expected_actions=("hold_downstream", "open_upstream_ticket"),
            note="SFTP drop delayed by an upstream outage.",
        ),
        ScenarioDay(
            business_date=day(5),
            faults={
                "pos_sales": FaultSpec("pos_sales", FaultType.REFERENTIAL_BREAK, magnitude=0.2)
            },
            dataset="pos_sales",
            expected_root_cause=RootCause.REFERENTIAL_BREAK,
            expected_actions=("quarantine_invalid_rows",),
            note="New items activated before the product master was synchronised.",
        ),
        ScenarioDay(
            business_date=day(6),
            faults={"inventory": drift},
            dataset="inventory",
            expected_root_cause=RootCause.SCHEMA_DRIFT,
            expected_actions=("add_column_mapping",),
            note="Source system renamed on_hand_cases in a Friday release.",
        ),
        ScenarioDay(
            business_date=day(7),
            faults={"inventory": drift},
            note="Control day: the rename persists, so a clean run proves the fix held.",
        ),
    )
    return Scenario(history_start=history_start, history_end=history_end, days=days)


DEFAULT_SCENARIO = build_scenario()
