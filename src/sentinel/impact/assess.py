"""Impact assessment: blast radius and a rough loss estimate for one incident.

Runs when an incident opens, before anyone diagnoses it, because the two
questions it answers are the ones a person asks first: who is looking at this
data, and how much does it matter.

The money figure is an estimate with stated assumptions and is presented as
one everywhere it appears. It is built from three terms, each a configured rate
times a count Sentinel does know:

* per hour - what each affected consumer costs for every hour it runs on bad
  data, times the hours the data is assumed to stay bad;
* per consumer - a flat cost of telling each owner and re-checking the asset;
* per record - the cost of reprocessing each record that is wrong or missing.

Nothing here measures money. Change the rates in the lineage file and the
estimate changes with them; what it is good for is ranking incidents against
each other, not booking a loss.
"""

from __future__ import annotations

from sentinel.config import Settings
from sentinel.impact.lineage import CostModel, LineageGraph, load_lineage
from sentinel.incidents.models import (
    AffectedAsset,
    CostEstimate,
    CostLine,
    ImpactAssessment,
    Incident,
)

#: Estimates are rounded to this many currency units; finer would be false precision.
ROUND_TO = 10


def assess_impact(
    incident: Incident, settings: Settings, graph: LineageGraph | None = None
) -> ImpactAssessment:
    """Work out who is downstream of an incident and what is roughly at stake.

    Args:
        incident: The incident, with its evidence and anomaly signals attached.
        settings: Settings; ``settings.impact.config_path`` selects the lineage file.
        graph: An already loaded graph, to skip reading the file.

    Raises:
        ImpactConfigError: If the lineage file cannot be used.
    """
    graph = graph or load_lineage(settings.impact.config_path)
    assets = graph.downstream(incident.dataset)
    records = records_at_risk(incident)
    cost = estimate_cost(
        graph.cost_model,
        dataset=incident.dataset,
        severity=incident.severity,
        assets=assets,
        records=records,
    )
    if not graph.knows(incident.dataset):
        cost.assumptions.insert(
            0,
            f"{incident.dataset} is not declared in the lineage file, so no "
            "downstream consumers are counted.",
        )
    return ImpactAssessment(
        dataset=incident.dataset,
        assets=assets,
        cost=cost,
        priority=priority_for(cost.amount, graph.cost_model),
        source=graph.source,
    )


def records_at_risk(incident: Incident) -> int:
    """Rows of the partition that may be wrong or missing.

    The whole partition counts: until a diagnosis narrows it down, every row of a
    partition that failed a check is suspect. When rows are missing, the count
    the detector expected for the weekday stands in for the ones that never came.
    """
    loaded = _as_int(incident.evidence.get("rows_loaded"))
    expected = 0
    for anomaly in incident.anomalies:
        if anomaly.get("metric") == "row_count":
            expected = _as_int(anomaly.get("expected"))
            break
    return max(loaded, expected)


def estimate_cost(
    model: CostModel,
    dataset: str,
    severity: str,
    assets: list[AffectedAsset],
    records: int,
) -> CostEstimate:
    """Turn counts into money using the configured rates."""
    consumers = [asset for asset in assets if asset.kind != "table"]
    hourly = sum(asset.cost_per_hour for asset in consumers)
    factor = model.severity_factor.get(severity, 1.0)
    record_rate = model.record_rate(dataset)

    lines = [
        CostLine(
            label="consumers running on bad data",
            amount=hourly * model.exposure_hours * factor,
            basis=(
                f"{len(consumers)} consumer(s) at {hourly:,.0f}/h combined "
                f"x {model.exposure_hours:g} h x {factor:g} ({severity})"
            ),
        ),
        CostLine(
            label="notifying owners and re-checking",
            amount=len(consumers) * model.cost_per_consumer,
            basis=f"{len(consumers)} consumer(s) x {model.cost_per_consumer:,.0f}",
        ),
        CostLine(
            label="reprocessing records",
            amount=records * record_rate,
            basis=f"{records:,} record(s) x {record_rate:g}",
        ),
    ]
    total = sum(line.amount for line in lines)
    for line in lines:
        line.amount = round(line.amount, 2)

    return CostEstimate(
        currency=model.currency,
        amount=_rounded(total),
        low=_rounded(total * (1 - model.uncertainty)),
        high=_rounded(total * (1 + model.uncertainty)),
        exposure_hours=model.exposure_hours,
        records_at_risk=records,
        components=lines,
        assumptions=[
            "This is an estimate from configured rates, not a measured loss.",
            f"Consumers are assumed to run on the bad data for {model.exposure_hours:g} "
            "hours before it is put right.",
            "Hourly rates come from each consumer's kind and criticality in the lineage "
            "file, unless the file sets one for it.",
            f"A {severity} incident is charged {factor:.0%} of the hourly rates.",
            "Every row of the partition counts as at risk, including rows that "
            "were expected and never arrived.",
            f"The range is the point estimate plus or minus {model.uncertainty:.0%}.",
        ],
    )


def priority_for(amount: float, model: CostModel) -> str:
    """Rank an estimate against the configured thresholds."""
    if amount >= model.priority_thresholds.p1:
        return "P1"
    if amount >= model.priority_thresholds.p2:
        return "P2"
    return "P3"


def format_money(amount: float, currency: str = "USD") -> str:
    """Render an amount the way the console and reports show it."""
    if currency.upper() == "USD":
        return f"${amount:,.0f}"
    return f"{amount:,.0f} {currency}"


def headline(impact: ImpactAssessment) -> str:
    """One line: priority, how many consumers of which kind, and the estimate."""
    consumers = impact.consumers
    kinds = ", ".join(
        f"{count} {kind}{'s' if count != 1 else ''}"
        for kind in ("decision", "dashboard", "report", "model")
        if (count := impact.count(kind))
    )
    cost = impact.cost
    return (
        f"{impact.priority} | {len(consumers)} downstream consumer(s)"
        f"{f' ({kinds})' if kinds else ''} | estimated loss "
        f"~{format_money(cost.amount, cost.currency)} "
        f"(range {format_money(cost.low, cost.currency)}-"
        f"{format_money(cost.high, cost.currency)})"
    )


def _rounded(amount: float) -> float:
    return float(round(amount / ROUND_TO) * ROUND_TO)


def _as_int(value: object) -> int:
    try:
        return max(0, round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
