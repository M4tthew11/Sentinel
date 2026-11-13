"""Hard rules: what always needs a human, whatever any reviewer says.

A reviewer is a judgement, and a judgement can be wrong, talked round or simply
unavailable. The rules here are none of those things: they are plain comparisons
on the action and the incident, evaluated in the executor, and a reviewer's
"approve" cannot lift them.

Two kinds of rule:

* **Risk.** A ``RiskLevel.HIGH`` action rewrites reported business figures. It
  is never applied without a human.
* **Scope.** An incident is about one dataset on one business date. An action
  that reaches for another dataset or another date is acting on something
  nobody has examined, so it is a human's call.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sentinel.incidents.models import Incident, RiskLevel
from sentinel.remediation.actions import ActionDefinition


def hard_rules(
    definition: ActionDefinition, arguments: dict[str, Any], incident: Incident
) -> list[str]:
    """The rules this action trips, as short phrases; empty when it trips none."""
    tripped: list[str] = []

    if definition.risk is RiskLevel.HIGH:
        tripped.append(f"risk={definition.risk}")

    dataset = arguments.get("dataset")
    if dataset not in (None, "") and str(dataset) != incident.dataset:
        tripped.append(f"dataset={dataset} is outside the incident's scope ({incident.dataset})")

    raw_date = arguments.get("business_date")
    if raw_date not in (None, ""):
        target = _as_date(raw_date)
        if target is None:
            tripped.append(f"business_date={raw_date!r} is not a date")
        elif target != incident.business_date:
            tripped.append(
                f"business_date={target.isoformat()} is outside the incident's scope "
                f"({incident.business_date.isoformat()})"
            )

    return tripped


def _as_date(raw: Any) -> date | None:
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None
