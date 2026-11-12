"""Command line interface.

``sentinel demo`` is the entry point a reviewer should use: it builds a warehouse
from scratch, injects one realistic fault per day, and shows the platform
detecting, diagnosing and repairing each one.
"""

from __future__ import annotations

import shutil
import sys
from datetime import date
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sentinel import __version__
from sentinel.agent.base import build_engine
from sentinel.config import AgentProvider, Settings, get_settings
from sentinel.elt.warehouse import Warehouse, open_warehouse
from sentinel.generator.source import generate_day, generate_range
from sentinel.incidents.models import ActionStatus, Incident
from sentinel.incidents.store import IncidentStore
from sentinel.logging import configure_logging
from sentinel.monitor import MonitorResult, monitor
from sentinel.remediation.executor import RemediationExecutor
from sentinel.runner import refresh_run
from sentinel.scenario import DEFAULT_SCENARIO, Scenario

app = typer.Typer(
    name="sentinel",
    help="AI-powered self-healing data platform.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

STATUS_STYLE = {
    "resolved": "green",
    "remediated": "yellow",
    "awaiting_approval": "magenta",
    "diagnosed": "cyan",
    "open": "red",
    "dismissed": "dim",
}


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show INFO logs.")] = False,
) -> None:
    """Configure logging and output encoding before any command runs."""
    # Windows consoles still default to a legacy code page; without this, a box
    # character in a table is enough to kill the demo with a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    settings = get_settings()
    configure_logging("INFO" if verbose else "WARNING", settings.log_format, force=True)


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------


@app.command()
def demo(
    fresh: Annotated[bool, typer.Option(help="Delete any existing warehouse first.")] = True,
    approve: Annotated[
        bool, typer.Option(help="Approve high-risk actions, as an on-call engineer would.")
    ] = True,
    rules_only: Annotated[
        bool, typer.Option(help="Force the deterministic engine even if an API key is set.")
    ] = False,
) -> None:
    """Run the full scenario end to end: history, faults, detection, repair."""
    settings = get_settings()
    if rules_only:
        settings.agent.provider = AgentProvider.RULES
    if fresh:
        _reset(settings)

    scenario = DEFAULT_SCENARIO
    engine = build_engine(settings)
    console.print(
        Panel.fit(
            f"[bold]Sentinel {__version__}[/bold]\n"
            f"diagnosis engine: [cyan]{engine.name}[/cyan] "
            f"({settings.agent.model if engine.name == 'claude' else 'offline decision tree'})\n"
            f"remediation mode: [cyan]{settings.remediation.mode}[/cyan]\n"
            f"warehouse: {settings.warehouse_path}",
            title="self-healing data platform",
        )
    )

    with open_warehouse(settings) as warehouse:
        _seed_history(warehouse, settings, scenario)
        for day in scenario.days:
            generate_day(day.business_date, day.faults, settings)
            result = monitor(
                day.business_date, warehouse, settings, triggered_by="demo", engine=engine
            )
            _render_day(result, day.note)
            if approve:
                _auto_approve(warehouse, settings, result)

        _render_summary(warehouse, scenario)


def _seed_history(warehouse: Warehouse, settings: Settings, scenario: Scenario) -> None:
    """Generate and process the clean warm-up period.

    Warm-up runs the pipeline and metric collection but not the incident loop:
    it exists to give the detector a baseline, and calling an LLM 42 times to
    confirm that nothing is wrong would be an expensive way to prove it.
    """
    dates = scenario.history_dates
    console.print(
        f"Building [bold]{len(dates)}[/bold] days of clean history ({dates[0]} to {dates[-1]})..."
    )
    generate_range(scenario.history_start, scenario.history_end, settings=settings)
    with typer.progressbar(dates, label="warm-up") as progress:
        for business_date in progress:
            refresh_run(business_date, warehouse, settings, "history", detect=False)
    console.print("[green]History ready.[/green]\n")


def _render_day(result: MonitorResult, note: str) -> None:
    """Print what happened on one business date."""
    header = f"[bold]{result.business_date}[/bold]"
    if not result.incidents:
        console.print(f"{header}  [green]clean run[/green] - no checks failed, no anomalies")
        if note:
            console.print(f"          [dim]{note}[/dim]")
        console.print()
        return

    console.print(f"{header}  [dim]{note}[/dim]")
    for incident in result.incidents:
        _render_incident(incident, compact=True)
    console.print()


def _render_incident(incident: Incident, compact: bool = False) -> None:
    """Print an incident, its diagnosis and the actions taken."""
    status = str(incident.status)
    console.print(
        f"  [{STATUS_STYLE.get(status, 'white')}]*[/] "
        f"[bold]{incident.incident_id}[/bold] {incident.dataset} "
        f"[{'red' if incident.severity == 'critical' else 'yellow'}]{incident.severity}[/] "
        f"-> [{STATUS_STYLE.get(status, 'white')}]{status}[/]"
    )
    console.print(f"      {incident.title}")

    if incident.diagnosis:
        diagnosis = incident.diagnosis
        console.print(
            f"      [cyan]diagnosis[/cyan] {diagnosis.root_cause} "
            f"(confidence {diagnosis.confidence:.0%}, {diagnosis.engine}, "
            f"{diagnosis.tool_calls} tool call(s), {diagnosis.latency_ms} ms)"
        )
        console.print(f"      {diagnosis.summary}")
        if not compact and diagnosis.reasoning:
            console.print(f"      [dim]{diagnosis.reasoning}[/dim]")

    for index, action in enumerate(incident.actions):
        marker = {
            ActionStatus.EXECUTED: "[green]OK[/green]",
            ActionStatus.AWAITING_APPROVAL: "[magenta]?[/magenta]",
            ActionStatus.REJECTED: "[dim]x[/dim]",
            ActionStatus.FAILED: "[red]![/red]",
        }.get(action.status, "|")
        console.print(f"      {marker} [{index}] {action.name} - {action.result}")


def _auto_approve(warehouse: Warehouse, settings: Settings, result: MonitorResult) -> None:
    """Approve every pending action, standing in for the on-call engineer."""
    store = IncidentStore(warehouse)
    executor = RemediationExecutor(warehouse, settings)
    for incident in result.awaiting_approval:
        pending = [
            index
            for index, action in enumerate(incident.actions)
            if action.status is ActionStatus.AWAITING_APPROVAL
        ]
        for index in pending:
            console.print(
                f"      [magenta]approval required[/magenta] for "
                f"{incident.actions[index].name} - approving as 'demo-operator'"
            )
            executor.approve(incident, index, "demo-operator")
        store.save(incident)
        _render_incident(incident, compact=True)


def _render_summary(warehouse: Warehouse, scenario: Scenario) -> None:
    """Print the scoreboard at the end of the demo."""
    store = IncidentStore(warehouse)
    incidents = store.search(limit=200)
    table = Table(title="incident summary", header_style="bold")
    for column in ("incident", "date", "dataset", "root cause", "expected", "status"):
        table.add_column(column)

    expected_by_date = {
        day.business_date: day.expected_root_cause for day in scenario.incident_days
    }
    for incident in sorted(incidents, key=lambda item: item.business_date):
        diagnosed = str(incident.diagnosis.root_cause) if incident.diagnosis else "-"
        expected = expected_by_date.get(incident.business_date)
        expected_text = str(expected) if expected else "-"
        match = "green" if expected and diagnosed == str(expected) else "yellow"
        table.add_row(
            incident.incident_id,
            str(incident.business_date),
            incident.dataset,
            f"[{match}]{diagnosed}[/{match}]",
            expected_text,
            f"[{STATUS_STYLE.get(str(incident.status), 'white')}]{incident.status}[/]",
        )
    console.print(table)

    held = warehouse.fetch_df(
        "SELECT business_date, dataset, reason FROM ops_hold WHERE released_at IS NULL"
    )
    tickets = int(warehouse.scalar("SELECT count(*) FROM ops_ticket") or 0)
    console.print(
        f"\n{len(incidents)} incident(s) | {tickets} upstream ticket(s) raised | "
        f"{len(held)} business date(s) still held from publication"
    )


# --------------------------------------------------------------------------
# Individual operations
# --------------------------------------------------------------------------


@app.command()
def generate(
    start: Annotated[str, typer.Option(help="First business date, YYYY-MM-DD.")],
    end: Annotated[str | None, typer.Option(help="Last business date, defaults to start.")] = None,
) -> None:
    """Generate clean source extracts for a date range."""
    settings = get_settings()
    first = date.fromisoformat(start)
    last = date.fromisoformat(end) if end else first
    results = generate_range(first, last, settings=settings)
    rows = sum(extract.row_count for day in results.values() for extract in day.values())
    console.print(f"Generated {len(results)} day(s), {rows:,} rows into {settings.landing_dir}")


@app.command()
def run(
    business_date: Annotated[str, typer.Argument(help="Business date, YYYY-MM-DD.")],
) -> None:
    """Run the pipeline and quality checks for one date, without the agent."""
    settings = get_settings()
    target = date.fromisoformat(business_date)
    with open_warehouse(settings) as warehouse:
        observation = refresh_run(target, warehouse, settings, "cli")
    failed = [
        check for results in observation.checks.values() for check in results if not check.passed
    ]
    console.print(
        f"{target}: pipeline [bold]{observation.pipeline.status}[/bold], "
        f"{len(failed)} failed check(s), "
        f"gold {'published' if observation.pipeline.gold_published else 'held'}"
    )
    for check in failed:
        console.print(f"  [red]x[/red] {check.dataset}.{check.check_name}: {check.details}")


@app.command("monitor")
def monitor_command(
    business_date: Annotated[str, typer.Argument(help="Business date, YYYY-MM-DD.")],
    remediate: Annotated[bool, typer.Option(help="Apply the proposed actions.")] = True,
) -> None:
    """Run, detect, diagnose and (optionally) repair one business date."""
    settings = get_settings()
    target = date.fromisoformat(business_date)
    with open_warehouse(settings) as warehouse:
        result = monitor(target, warehouse, settings, "cli", remediate=remediate)
        _render_day(result, "")


@app.command()
def incidents(
    business_date: Annotated[str | None, typer.Option("--date", help="Filter by date.")] = None,
    limit: Annotated[int, typer.Option(help="Maximum rows.")] = 25,
) -> None:
    """List incidents, newest first."""
    settings = get_settings()
    with open_warehouse(settings, read_only=True, init=False) as warehouse:
        store = IncidentStore(warehouse)
        rows = store.search(
            business_date=date.fromisoformat(business_date) if business_date else None,
            limit=limit,
        )

    if not rows:
        console.print("No incidents on record.")
        return

    table = Table(header_style="bold")
    for column in ("incident", "date", "dataset", "severity", "status", "root cause", "title"):
        table.add_column(column)
    for incident in rows:
        table.add_row(
            incident.incident_id,
            str(incident.business_date),
            incident.dataset,
            incident.severity,
            f"[{STATUS_STYLE.get(str(incident.status), 'white')}]{incident.status}[/]",
            str(incident.diagnosis.root_cause) if incident.diagnosis else "-",
            incident.title[:60],
        )
    console.print(table)


@app.command()
def show(incident_id: Annotated[str, typer.Argument(help="Incident id.")]) -> None:
    """Show one incident in full, including the agent's reasoning."""
    settings = get_settings()
    with open_warehouse(settings, read_only=True, init=False) as warehouse:
        incident = IncidentStore(warehouse).get(incident_id)
    if incident is None:
        console.print(f"[red]No incident {incident_id}[/red]")
        raise typer.Exit(code=1)

    _render_incident(incident)
    console.print("\n[bold]timeline[/bold]")
    for event in incident.timeline:
        console.print(f"  {event.at:%H:%M:%S}  {event.actor:<20} {event.event}: {event.detail}")


@app.command()
def approve(
    incident_id: Annotated[str, typer.Argument(help="Incident id.")],
    action: Annotated[int, typer.Option(help="Index of the action to approve.")] = 0,
    by: Annotated[str, typer.Option(help="Who is approving.")] = "operator",
) -> None:
    """Approve a pending high-risk action."""
    settings = get_settings()
    with open_warehouse(settings) as warehouse:
        store = IncidentStore(warehouse)
        incident = store.get(incident_id)
        if incident is None:
            console.print(f"[red]No incident {incident_id}[/red]")
            raise typer.Exit(code=1)
        RemediationExecutor(warehouse, settings).approve(incident, action, by)
        store.save(incident)
        _render_incident(incident)


@app.command()
def reject(
    incident_id: Annotated[str, typer.Argument(help="Incident id.")],
    action: Annotated[int, typer.Option(help="Index of the action to reject.")] = 0,
    reason: Annotated[str, typer.Option(help="Why it was rejected.")] = "rejected by operator",
    by: Annotated[str, typer.Option(help="Who is rejecting.")] = "operator",
) -> None:
    """Reject a pending action."""
    settings = get_settings()
    with open_warehouse(settings) as warehouse:
        store = IncidentStore(warehouse)
        incident = store.get(incident_id)
        if incident is None:
            console.print(f"[red]No incident {incident_id}[/red]")
            raise typer.Exit(code=1)
        RemediationExecutor(warehouse, settings).reject(incident, action, by, reason)
        store.save(incident)
        _render_incident(incident)


@app.command("release-hold")
def release_hold_command(
    hold_id: Annotated[str, typer.Argument(help="Hold id, e.g. HOLD-1a2b3c4d.")],
) -> None:
    """Release a publication hold and rebuild that business date."""
    from sentinel.elt import ops

    settings = get_settings()
    with open_warehouse(settings) as warehouse:
        holds = {hold.hold_id: hold for hold in ops.active_holds(warehouse)}
        hold = holds.get(hold_id)
        if hold is None:
            console.print(f"[red]No active hold {hold_id}[/red]")
            raise typer.Exit(code=1)
        ops.release_hold(warehouse, hold_id)
        observation = refresh_run(hold.business_date, warehouse, settings, "release-hold")
    console.print(
        f"Released {hold_id}; {hold.business_date} rebuilt "
        f"({'published' if observation.pipeline.gold_published else 'still held'})"
    )


@app.command()
def status() -> None:
    """Show warehouse contents at a glance."""
    settings = get_settings()
    if not settings.warehouse_path.exists():
        console.print("No warehouse yet - run [bold]sentinel demo[/bold].")
        return

    with open_warehouse(settings, read_only=True, init=False) as warehouse:
        table = Table(header_style="bold")
        table.add_column("table")
        table.add_column("rows", justify="right")
        for name in (
            "bronze_pos_sales",
            "bronze_shipments",
            "bronze_inventory",
            "silver_pos_sales",
            "silver_shipments",
            "silver_inventory",
            "gold_sku_daily",
            "gold_brand_daily",
            "dq_metric",
            "dq_check_result",
            "ops_incident",
            "ops_ticket",
        ):
            if warehouse.table_exists(name):
                count = int(warehouse.scalar(f"SELECT count(*) FROM {name}") or 0)
                table.add_row(name, f"{count:,}")
        console.print(table)


@app.command("eval")
def eval_command(
    rules_only: Annotated[bool, typer.Option(help="Score the deterministic engine.")] = False,
    keep: Annotated[bool, typer.Option(help="Reuse an existing warehouse.")] = False,
    min_accuracy: Annotated[
        float,
        typer.Option(
            help="Exit non-zero when root-cause accuracy falls below this. Used as a CI gate."
        ),
    ] = 0.0,
    scenario: Annotated[
        str,
        typer.Option(
            help="Which scenario to replay: 'standard' (one catalogued fault per day) or "
            "'hard' (compound and off-catalogue faults)."
        ),
    ] = "standard",
    repeat: Annotated[
        int,
        typer.Option(
            min=1,
            max=20,
            help="Replay the scenario this many times and report the mean and spread. "
            "Worth it for the LLM engine, whose answers vary between runs.",
        ),
    ] = 1,
) -> None:
    """Score the diagnosis engine against the golden incident set."""
    from sentinel.evals.harness import (
        render_repeated,
        render_report,
        run_evals,
        run_repeated_evals,
    )
    from sentinel.evals.reports import EVAL_REPORT_DIR, save_report
    from sentinel.scenario import SCENARIOS

    if scenario not in SCENARIOS:
        raise typer.BadParameter(
            f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}",
            param_hint="--scenario",
        )
    settings = get_settings()
    if rules_only:
        settings.agent.provider = AgentProvider.RULES
    if repeat > 1 and keep:
        raise typer.BadParameter("--keep cannot be combined with --repeat", param_hint="--keep")

    if repeat == 1:
        report = run_evals(settings, scenario=SCENARIOS[scenario](), fresh=not keep)
        render_report(report, console)
        saved = save_report(report, settings.data_dir / EVAL_REPORT_DIR)
        worst = report.root_cause_accuracy
    else:
        repeated = run_repeated_evals(settings, scenario=SCENARIOS[scenario](), repeat=repeat)
        render_repeated(repeated, console)
        saved = save_report(repeated, settings.data_dir / EVAL_REPORT_DIR)
        # A gate is only as good as its worst run.
        worst = repeated.spread("root_cause_accuracy")[0]
    console.print(f"[dim]report saved to {saved}; the console shows it under Evals[/dim]")

    if worst < min_accuracy:
        console.print(
            f"[red]FAIL[/red] root-cause accuracy {worst:.0%} "
            f"is below the required {min_accuracy:.0%}"
        )
        raise typer.Exit(code=1)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
) -> None:
    """Serve the read API and dashboard."""
    import uvicorn

    uvicorn.run("sentinel.api.main:app", host=host, port=port, log_level="info")


@app.command()
def reset(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
) -> None:
    """Delete the local warehouse and landing zone."""
    settings = get_settings()
    if not yes:
        typer.confirm(f"Delete {settings.data_dir} and everything in it?", abort=True)
    _reset(settings)
    console.print(f"Removed {settings.data_dir}")


def _reset(settings: Settings) -> None:
    """Remove all local runtime state."""
    if settings.data_dir.exists():
        shutil.rmtree(settings.data_dir)
    settings.ensure_dirs()


if __name__ == "__main__":  # pragma: no cover
    app()
