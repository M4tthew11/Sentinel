"""What an action would change, worked out before anyone approves it.

An approver looking at ``apply_unit_correction(factor=0.0833)`` is being asked to
rewrite a day of reported sales. The useful question is not "is the factor
plausible" but "what will the number be afterwards, and is that a normal day?".
The preview answers it from the metrics the platform already records, so it
never touches silver and cannot change anything.
"""

from __future__ import annotations

from typing import Any

from sentinel.detection.detector import baseline_for
from sentinel.elt.warehouse import Warehouse
from sentinel.generator.faults import VOLUME_GROUP_COLUMN
from sentinel.incidents.models import ActionRecord, ActionStatus, Incident
from sentinel.quality import metrics as m
from sentinel.remediation.actions import ACTIONS


def preview(warehouse: Warehouse, incident: Incident, action: ActionRecord) -> dict[str, Any]:
    """A JSON-ready description of one proposed action's effect."""
    definition = ACTIONS.get(action.name)
    payload: dict[str, Any] = {
        "name": action.name,
        "status": str(action.status),
        "risk": str(action.risk),
        "arguments": action.arguments,
        "description": definition.description if definition else "Not in the whitelist.",
        "rebuilds_silver": definition.triggers_rebuild if definition else False,
        "effects": [],
    }
    if definition is None:
        payload["effects"].append("This action is not whitelisted and will never run.")
        return payload
    if action.status not in (ActionStatus.PROPOSED, ActionStatus.AWAITING_APPROVAL):
        # Recorded metrics already reflect an executed action; working the effect
        # out again would apply it twice on paper.
        payload["effects"].append(
            f"Already {action.status}: {action.result}"
            if action.result
            else f"Already {action.status}."
        )
        return payload

    dataset = str(action.arguments.get("dataset") or incident.dataset)
    current = m.latest_metrics(warehouse, dataset, incident.business_date)
    rows = current.get(m.ROW_COUNT)

    if action.name == "apply_unit_correction":
        payload["effects"].extend(
            _unit_correction(warehouse, incident, dataset, action.arguments, current)
        )
    elif action.name == "enable_dedupe":
        duplicate_rate = current.get(m.DUPLICATE_RATE)
        if rows is not None and duplicate_rate is not None:
            payload["effects"].append(
                f"About {round(rows * duplicate_rate):,} of {rows:,.0f} rows in {dataset} on "
                f"{incident.business_date} share a natural key with another row and would be "
                "dropped. Dedupe stays on for later dates too."
            )
    elif action.name == "quarantine_invalid_rows":
        payload["effects"].append(
            f"Rows in {dataset} that break the contract move to silver_{dataset}_rejected; "
            "the rest of the partition is published. Stays on for later dates too."
        )
    elif action.name == "quarantine_partition":
        payload["effects"].append(
            f"All {rows:,.0f} rows of {dataset} on {incident.business_date} leave silver."
            if rows is not None
            else f"The whole {dataset} partition for {incident.business_date} leaves silver."
        )
    elif action.name == "hold_downstream":
        payload["effects"].append(
            f"Gold is not published for {incident.business_date} until the hold is released. "
            "Silver and other dates are unaffected."
        )
    elif action.name == "add_column_mapping":
        payload["effects"].append(
            f"{dataset}.{action.arguments.get('canonical_column')} is read from source column "
            f"{action.arguments.get('source_column')!r} from now on."
        )
    elif action.name == "open_upstream_ticket":
        payload["effects"].append("A ticket is recorded. No data changes.")
    return payload


def _unit_correction(
    warehouse: Warehouse,
    incident: Incident,
    dataset: str,
    arguments: dict[str, Any],
    current: dict[str, float],
) -> list[str]:
    column = str(arguments.get("column_name") or "")
    try:
        factor = float(arguments.get("factor"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return [f"Factor {arguments.get('factor')!r} is not a number; this will fail."]

    metric = f"{m.MEASURE_SUM_PREFIX}{column}"
    observed = current.get(metric)
    if observed is None:
        return [f"No recorded total for {dataset}.{column} on {incident.business_date}."]

    corrected = observed * factor
    effects = [
        f"Every {column} value in {dataset} on {incident.business_date} is multiplied by "
        f"{factor:g}: the day's total goes from {observed:,.2f} to {corrected:,.2f}."
    ]
    expected = baseline_for(warehouse, dataset, incident.business_date, metric)
    if expected:
        effects.append(
            f"A typical {incident.business_date:%A} is about {expected:,.2f}, so the corrected "
            f"total would be {corrected / expected:.0%} of normal."
        )
    effects.extend(_per_group_check(warehouse, incident, dataset, column, factor))
    return effects


#: A reporting unit whose corrected value lands outside this band of its own
#: value a week earlier is counted as distorted by the correction.
GROUP_BAND = (0.5, 2.0)


def _per_group_check(
    warehouse: Warehouse, incident: Incident, dataset: str, column: str, factor: float
) -> list[str]:
    """Whether one factor fits every store (or DC), not just the day's total.

    A factor derived from the total always makes the total look right. If only
    some stores changed unit, the same factor shrinks every other store, and
    that only shows up store by store.
    """
    group = VOLUME_GROUP_COLUMN.get(dataset)
    if group is None or not column.isidentifier():
        return []
    try:
        rows = warehouse.fetch_records(
            f"""
            WITH today AS (
                SELECT {group} AS unit, sum({column}) AS value
                FROM silver_{dataset} WHERE _business_date = ? GROUP BY 1
            ),
            last_week AS (
                SELECT {group} AS unit, sum({column}) AS value
                FROM silver_{dataset} WHERE _business_date = ? - INTERVAL 7 DAY GROUP BY 1
            )
            SELECT today.value AS today, last_week.value AS last_week
            FROM today JOIN last_week USING (unit)
            WHERE last_week.value > 0
            """,
            [incident.business_date, incident.business_date],
        )
    except Exception:  # a preview must never fail the page
        return []
    if not rows:
        return []

    low, high = GROUP_BAND
    distorted = [
        row
        for row in rows
        if not low <= float(row["today"]) * factor / float(row["last_week"]) <= high
    ]
    label = "store" if group == "store_id" else "distribution centre"
    if not distorted:
        return [
            f"Checked {len(rows)} {label}s against the same day last week: the factor fits "
            "every one of them."
        ]
    share = len(distorted) / len(rows)
    return [
        f"Warning: after correction, {len(distorted)} of {len(rows)} {label}s ({share:.0%}) "
        f"would be below half or above double their value a week earlier. One factor does "
        "not fit this partition; the unit change is probably not uniform."
    ]
