"""Scores a diagnosis engine against known incidents.

The ground truth is the injected fault, so this measures the thing that actually
matters: given only the evidence a real on-call engineer would have, does the
engine name the right root cause and propose the actions that fix it?

Both engines run through the same harness. That makes "should we use an LLM here
at all?" an empirical question rather than an architectural preference - and on a
one-day-per-fault set the decision tree is a genuinely strong baseline.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

from sentinel.agent.base import build_engine
from sentinel.config import Settings, get_settings
from sentinel.elt.warehouse import open_warehouse
from sentinel.generator.source import generate_day, generate_range
from sentinel.incidents.models import Incident, RootCause
from sentinel.logging import get_logger
from sentinel.monitor import monitor
from sentinel.runner import refresh_run
from sentinel.scenario import DEFAULT_SCENARIO, Scenario, ScenarioDay

log = get_logger(__name__)


@dataclass
class EvalCase:
    """The outcome of scoring one scenario day."""

    business_date: str
    dataset: str
    expected_root_cause: str
    observed_root_cause: str
    expected_actions: tuple[str, ...]
    proposed_actions: tuple[str, ...]
    incident_opened: bool
    confidence: float = 0.0
    tool_calls: int = 0
    latency_ms: int = 0
    expected_contributing: tuple[str, ...] = ()
    observed_contributing: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    final_status: str = "-"

    @property
    def expects_incident(self) -> bool:
        """Whether an incident should have been opened at all."""
        return self.expected_root_cause != "none"

    @property
    def detected(self) -> bool:
        """Whether detection behaved correctly (fired iff it should have)."""
        return self.incident_opened == self.expects_incident

    @property
    def expected_causes(self) -> tuple[str, ...]:
        """Every cause injected into the partition, primary first."""
        if not self.expects_incident:
            return ()
        return (self.expected_root_cause, *self.expected_contributing)

    @property
    def root_cause_correct(self) -> bool:
        """Whether the engine's primary cause is one of the injected faults.

        On a compound day either injected fault is an acceptable headline; naming
        the second one as well is scored separately, by :attr:`cause_coverage`.
        """
        return self.expects_incident and self.observed_root_cause in self.expected_causes

    @property
    def cause_coverage(self) -> float:
        """Share of the injected causes the engine named, primary or contributing."""
        if not self.expects_incident:
            return 1.0
        named = {self.observed_root_cause, *self.observed_contributing}
        return sum(cause in named for cause in self.expected_causes) / len(self.expected_causes)

    @property
    def harmful(self) -> bool:
        """Whether the engine proposed an action that would make this day worse."""
        return bool(set(self.forbidden_actions) & set(self.proposed_actions))

    @property
    def actions_correct(self) -> bool:
        """Every action a competent engineer would take, and none that does harm."""
        if not self.expects_incident:
            return not self.proposed_actions
        return set(self.expected_actions).issubset(set(self.proposed_actions)) and not self.harmful


@dataclass
class EvalReport:
    """Aggregate results across the scenario."""

    engine: str
    scenario: str = "standard"
    cases: list[EvalCase] = field(default_factory=list)
    wall_seconds: float = 0.0

    @property
    def incident_cases(self) -> list[EvalCase]:
        """Cases where a fault was actually injected."""
        return [case for case in self.cases if case.expects_incident]

    @property
    def detection_rate(self) -> float:
        """Share of days where detection fired correctly (including true negatives)."""
        return _ratio([case.detected for case in self.cases])

    @property
    def root_cause_accuracy(self) -> float:
        """Share of injected faults correctly identified."""
        return _ratio([case.root_cause_correct for case in self.incident_cases])

    @property
    def action_accuracy(self) -> float:
        """Share of cases where the expected remediation was proposed."""
        return _ratio([case.actions_correct for case in self.incident_cases])

    @property
    def cause_coverage(self) -> float:
        """Mean share of injected causes named, over incident cases."""
        values = [case.cause_coverage for case in self.incident_cases]
        return sum(values) / len(values) if values else 0.0

    @property
    def harmful_rate(self) -> float:
        """Share of incident cases where a damaging action was proposed."""
        return _ratio([case.harmful for case in self.incident_cases])

    @property
    def mean_latency_ms(self) -> float:
        """Average diagnosis latency over incident cases."""
        values = [case.latency_ms for case in self.incident_cases]
        return sum(values) / len(values) if values else 0.0

    @property
    def mean_tool_calls(self) -> float:
        """Average number of investigation tool calls per diagnosis."""
        values = [case.tool_calls for case in self.incident_cases]
        return sum(values) / len(values) if values else 0.0


def _ratio(flags: list[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0


def run_evals(
    settings: Settings | None = None,
    scenario: Scenario | None = None,
    fresh: bool = True,
) -> EvalReport:
    """Replay the scenario and score every day.

    Runs against its own warehouse under ``<data_dir>/eval`` so a scoring run
    never disturbs the demo warehouse.
    """
    base = settings or get_settings()
    scenario = scenario or DEFAULT_SCENARIO
    eval_settings = base.model_copy(update={"data_dir": base.data_dir / "eval"})

    if fresh and eval_settings.data_dir.exists():
        shutil.rmtree(eval_settings.data_dir)
    eval_settings.ensure_dirs()

    engine = build_engine(eval_settings)
    report = EvalReport(engine=engine.name, scenario=scenario.name)
    started = time.perf_counter()

    with open_warehouse(eval_settings) as warehouse:
        generate_range(scenario.history_start, scenario.history_end, settings=eval_settings)
        for business_date in scenario.history_dates:
            refresh_run(business_date, warehouse, eval_settings, "eval-history", detect=False)

        for day in scenario.days:
            generate_day(day.business_date, day.faults, eval_settings)
            result = monitor(
                day.business_date,
                warehouse,
                eval_settings,
                triggered_by="eval",
                engine=engine,
            )
            report.cases.append(_score(day, result.incidents))

    report.wall_seconds = time.perf_counter() - started
    log.info(
        "evals.finished",
        engine=report.engine,
        root_cause_accuracy=round(report.root_cause_accuracy, 3),
        action_accuracy=round(report.action_accuracy, 3),
    )
    return report


def _score(day: ScenarioDay, incidents: list[Incident]) -> EvalCase:
    """Compare one day's incidents against the injected fault."""
    expected = str(day.expected_root_cause) if day.expected_root_cause else "none"
    relevant = [item for item in incidents if day.dataset is None or item.dataset == day.dataset]
    incident = relevant[0] if relevant else None

    observed = str(RootCause.UNKNOWN)
    contributing: tuple[str, ...] = ()
    proposed: tuple[str, ...] = ()
    confidence = 0.0
    tool_calls = 0
    latency = 0

    if incident is not None and incident.diagnosis is not None:
        diagnosis = incident.diagnosis
        observed = str(diagnosis.root_cause)
        contributing = tuple(str(cause) for cause in diagnosis.contributing_causes)
        proposed = tuple(action.name for action in diagnosis.proposed_actions)
        confidence = diagnosis.confidence
        tool_calls = diagnosis.tool_calls
        latency = diagnosis.latency_ms
    elif incident is None:
        observed = "none"

    return EvalCase(
        business_date=day.business_date.isoformat(),
        dataset=day.dataset or "-",
        expected_root_cause=expected,
        observed_root_cause=observed,
        expected_actions=day.expected_actions,
        proposed_actions=proposed,
        incident_opened=incident is not None,
        confidence=confidence,
        tool_calls=tool_calls,
        latency_ms=latency,
        expected_contributing=tuple(str(cause) for cause in day.contributing_causes),
        observed_contributing=contributing,
        forbidden_actions=day.forbidden_actions,
        final_status=str(incident.status) if incident is not None else "-",
    )


def render_report(report: EvalReport, console: Console | None = None) -> None:
    """Print an eval report as a table."""
    console = console or Console()
    table = Table(
        title=f"diagnosis evals - scenario: {report.scenario}, engine: {report.engine}",
        header_style="bold",
    )
    for column in ("date", "dataset", "expected", "observed", "actions", "confidence", "status"):
        table.add_column(column)

    for case in report.cases:
        ok = "green" if case.root_cause_correct or not case.expects_incident else "red"
        actions = "green" if case.actions_correct else "yellow"
        if case.harmful:
            actions = "red"
        table.add_row(
            case.business_date,
            case.dataset,
            " + ".join(case.expected_causes) or "none",
            f"[{ok}]{' + '.join((case.observed_root_cause, *case.observed_contributing))}[/{ok}]",
            f"[{actions}]{', '.join(case.proposed_actions) or '-'}[/{actions}]",
            f"{case.confidence:.0%}" if case.expects_incident else "-",
            case.final_status,
        )
    console.print(table)
    console.print(
        f"detection {report.detection_rate:.0%} | "
        f"root cause {report.root_cause_accuracy:.0%} | "
        f"causes found {report.cause_coverage:.0%} | "
        f"actions {report.action_accuracy:.0%} | "
        f"harmful {report.harmful_rate:.0%} | "
        f"{report.mean_tool_calls:.1f} tool calls/diagnosis | "
        f"{report.mean_latency_ms:.0f} ms/diagnosis | "
        f"{report.wall_seconds:.1f}s total"
    )


#: The headline metrics, as (label, attribute) pairs, in display order.
HEADLINE_METRICS: tuple[tuple[str, str], ...] = (
    ("detection", "detection_rate"),
    ("root cause", "root_cause_accuracy"),
    ("causes found", "cause_coverage"),
    ("actions", "action_accuracy"),
    ("harmful", "harmful_rate"),
)


@dataclass
class RepeatedEvalReport:
    """Several replays of the same scenario with the same engine.

    The rule engine is deterministic, so one run says everything. An LLM is not:
    the same incident can be diagnosed differently on a second attempt, and a
    single good run is an anecdote. Means, the min-max spread and per-day tallies
    say how much of the result is the engine and how much is the dice.
    """

    runs: list[EvalReport]

    @property
    def engine(self) -> str:
        """The engine every run used."""
        return self.runs[0].engine

    @property
    def scenario(self) -> str:
        """The scenario every run replayed."""
        return self.runs[0].scenario

    def values(self, attribute: str) -> list[float]:
        """One headline metric across the runs."""
        return [float(getattr(run, attribute)) for run in self.runs]

    def mean(self, attribute: str) -> float:
        """Mean of a headline metric across the runs."""
        values = self.values(attribute)
        return sum(values) / len(values)

    def spread(self, attribute: str) -> tuple[float, float]:
        """Lowest and highest value of a headline metric across the runs."""
        values = self.values(attribute)
        return min(values), max(values)

    def day_tally(self, index: int, predicate: str) -> int:
        """In how many runs a boolean property of day ``index`` held."""
        return sum(bool(getattr(run.cases[index], predicate)) for run in self.runs)

    @property
    def wall_seconds(self) -> float:
        """Total time over all runs."""
        return sum(run.wall_seconds for run in self.runs)


def run_repeated_evals(
    settings: Settings | None = None,
    scenario: Scenario | None = None,
    repeat: int = 1,
) -> RepeatedEvalReport:
    """Replay a scenario ``repeat`` times from a fresh warehouse each time."""
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    runs = []
    for attempt in range(repeat):
        log.info("evals.attempt", attempt=attempt + 1, of=repeat)
        runs.append(run_evals(settings, scenario=scenario, fresh=True))
    return RepeatedEvalReport(runs=runs)


def render_repeated(report: RepeatedEvalReport, console: Console | None = None) -> None:
    """Print one line per run, then the mean and spread of each headline metric."""
    console = console or Console()
    table = Table(
        title=(f"{len(report.runs)} runs - scenario: {report.scenario}, engine: {report.engine}"),
        header_style="bold",
    )
    table.add_column("run")
    for label, _ in HEADLINE_METRICS:
        table.add_column(label)
    table.add_column("seconds")
    for number, run in enumerate(report.runs, start=1):
        table.add_row(
            str(number),
            *(f"{getattr(run, attribute):.0%}" for _, attribute in HEADLINE_METRICS),
            f"{run.wall_seconds:.0f}",
        )
    table.add_row(
        "[bold]mean[/bold]",
        *(f"[bold]{report.mean(attribute):.0%}[/bold]" for _, attribute in HEADLINE_METRICS),
        f"{report.wall_seconds:.0f}",
    )
    console.print(table)
    console.print(
        " | ".join(
            f"{label} {low:.0%}-{high:.0%}"
            for label, attribute in HEADLINE_METRICS
            for low, high in [report.spread(attribute)]
        )
        + "   (min-max across runs)"
    )
