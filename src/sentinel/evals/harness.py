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

    @property
    def expects_incident(self) -> bool:
        """Whether an incident should have been opened at all."""
        return self.expected_root_cause != "none"

    @property
    def detected(self) -> bool:
        """Whether detection behaved correctly (fired iff it should have)."""
        return self.incident_opened == self.expects_incident

    @property
    def root_cause_correct(self) -> bool:
        """Whether the engine named the injected fault."""
        return self.expects_incident and self.observed_root_cause == self.expected_root_cause

    @property
    def actions_correct(self) -> bool:
        """Whether every action a competent engineer would take was proposed."""
        if not self.expects_incident:
            return not self.proposed_actions
        return set(self.expected_actions).issubset(set(self.proposed_actions))


@dataclass
class EvalReport:
    """Aggregate results across the scenario."""

    engine: str
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
    report = EvalReport(engine=engine.name)
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
    proposed: tuple[str, ...] = ()
    confidence = 0.0
    tool_calls = 0
    latency = 0

    if incident is not None and incident.diagnosis is not None:
        diagnosis = incident.diagnosis
        observed = str(diagnosis.root_cause)
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
    )


def render_report(report: EvalReport, console: Console | None = None) -> None:
    """Print an eval report as a table."""
    console = console or Console()
    table = Table(title=f"diagnosis evals - engine: {report.engine}", header_style="bold")
    for column in ("date", "dataset", "expected", "observed", "actions", "confidence"):
        table.add_column(column)

    for case in report.cases:
        ok = "green" if case.root_cause_correct or not case.expects_incident else "red"
        actions = "green" if case.actions_correct else "yellow"
        table.add_row(
            case.business_date,
            case.dataset,
            case.expected_root_cause,
            f"[{ok}]{case.observed_root_cause}[/{ok}]",
            f"[{actions}]{', '.join(case.proposed_actions) or '-'}[/{actions}]",
            f"{case.confidence:.0%}" if case.expects_incident else "-",
        )
    console.print(table)
    console.print(
        f"detection {report.detection_rate:.0%} | "
        f"root cause {report.root_cause_accuracy:.0%} | "
        f"actions {report.action_accuracy:.0%} | "
        f"{report.mean_tool_calls:.1f} tool calls/diagnosis | "
        f"{report.mean_latency_ms:.0f} ms/diagnosis | "
        f"{report.wall_seconds:.1f}s total"
    )
