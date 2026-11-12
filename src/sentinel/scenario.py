"""Demo scenarios: a clean history followed by injected faults.

``standard`` injects one catalogued fault per day. ``hard`` is what the standard
one cannot measure: several faults on the same partition, and faults nobody wrote
a rule or a root-cause label for.

Used by three consumers, which is the point - the demo a reviewer runs, the eval
harness that scores the agent, and the integration tests all exercise the same
incidents, so a passing eval says something about the demo rather than about a
fixture written to make the eval pass.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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
    faults: Mapping[str, FaultSpec | Sequence[FaultSpec]] = field(default_factory=dict)
    dataset: str | None = None
    expected_root_cause: RootCause | None = None
    expected_actions: tuple[str, ...] = ()
    note: str = ""
    contributing_causes: tuple[RootCause, ...] = ()
    """Further causes injected into the same partition. An engine is credited
    with the root cause if it names any injected cause first, and is scored
    separately on how many of them it named at all."""

    forbidden_actions: tuple[str, ...] = ()
    """Actions that would make things worse here, such as a blanket unit
    correction when only some stores changed unit."""

    @property
    def expected_causes(self) -> tuple[RootCause, ...]:
        """Every cause injected into the partition, primary first."""
        if self.expected_root_cause is None:
            return ()
        return (self.expected_root_cause, *self.contributing_causes)

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
    name: str = "standard"

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

    def faults_by_date(self) -> dict[date, Mapping[str, FaultSpec | Sequence[FaultSpec]]]:
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


def build_hard_scenario(history_start: date = date(2025, 1, 6)) -> Scenario:
    """Faults that a decision tree written for one fault at a time gets wrong.

    The rule engine is not expected to pass this; it is the baseline that makes
    the comparison with an LLM engine mean something. Two kinds of day:

    * **Compound faults.** Two independent failures on one partition. A rule
      chain stops at its first match, fixes half the problem and - unless
      verification also looks at the metrics - reports the partition healthy.
    * **Off-catalogue faults.** Failures with no rule and no label of their own.
      The test is whether the engine contains them, and above all whether it
      avoids forcing them into a category whose fix does damage.
    """
    history_end = history_start + timedelta(days=HISTORY_DAYS - 1)
    first = history_end + timedelta(days=1)

    def day(offset: int) -> date:
        return first + timedelta(days=offset)

    days = (
        ScenarioDay(
            business_date=day(0),
            faults={
                "pos_sales": (
                    FaultSpec("pos_sales", FaultType.DUPLICATE_BATCH, magnitude=0.3),
                    FaultSpec(
                        "pos_sales",
                        FaultType.UNIT_OF_MEASURE,
                        column="units_sold",
                        metadata={"factor": "12"},
                    ),
                )
            },
            dataset="pos_sales",
            expected_root_cause=RootCause.DUPLICATE_BATCH,
            contributing_causes=(RootCause.UNIT_OF_MEASURE,),
            expected_actions=("enable_dedupe", "apply_unit_correction"),
            note="File delivered twice, and in cases instead of eaches.",
        ),
        ScenarioDay(
            business_date=day(1),
            faults={
                "shipments": (
                    FaultSpec("shipments", FaultType.VOLUME_DROP, magnitude=0.5),
                    FaultSpec("shipments", FaultType.REFERENTIAL_BREAK, magnitude=0.15),
                )
            },
            dataset="shipments",
            expected_root_cause=RootCause.REFERENTIAL_BREAK,
            contributing_causes=(RootCause.VOLUME_DROP,),
            expected_actions=("quarantine_invalid_rows", "hold_downstream"),
            note="Extract truncated halfway, and new SKUs ahead of the product master.",
        ),
        ScenarioDay(
            business_date=day(2),
            faults={
                "pos_sales": FaultSpec(
                    "pos_sales",
                    FaultType.PARTIAL_UNIT_CHANGE,
                    magnitude=0.3,
                    metadata={"factor": "12"},
                )
            },
            dataset="pos_sales",
            expected_root_cause=RootCause.UNIT_OF_MEASURE,
            expected_actions=("hold_downstream", "open_upstream_ticket"),
            forbidden_actions=("apply_unit_correction",),
            note="A POS release moved some stores to reporting cases; the rest did not.",
        ),
        ScenarioDay(
            business_date=day(3),
            faults={"pos_sales": FaultSpec("pos_sales", FaultType.NEGATED_RETURNS, magnitude=0.25)},
            dataset="pos_sales",
            expected_root_cause=RootCause.UNKNOWN,
            expected_actions=("hold_downstream", "open_upstream_ticket"),
            forbidden_actions=("apply_unit_correction",),
            note="Returns started arriving netted into sales as negative quantities.",
        ),
        ScenarioDay(
            business_date=day(4),
            note="Control day: nothing injected, nothing should fire.",
        ),
    )
    return Scenario(history_start=history_start, history_end=history_end, days=days, name="hard")


DEFAULT_SCENARIO = build_scenario()

#: Every scenario the CLI and the eval harness can replay, by name.
SCENARIOS: dict[str, Callable[[], Scenario]] = {
    "standard": build_scenario,
    "hard": build_hard_scenario,
}
