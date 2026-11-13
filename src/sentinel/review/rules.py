"""The deterministic reviewer.

For each action it asks one question: *do the monitor's own observations show the
problem this action exists to fix?* Deduplication is approved when the uniqueness
check failed, a column mapping when the canonical column is missing and the
proposed source column is really in the extract, and so on. It never reads the
diagnosis, so it agrees with the proposing agent only when the evidence does.

It is deliberately narrow. Anything it has no precondition for is escalated, and
so is anything whose precondition it cannot fully check - a rule that cannot tell
"all stores changed unit" from "some stores did" has no business approving a
correction. What it approves is small, and that is the design: the cost of an
unnecessary escalation is a human glancing at an action; the cost of a wrong
approval is wrong data.
"""

from __future__ import annotations

from typing import Any

from sentinel.incidents.models import ActionReview, Incident, ReviewVerdict
from sentinel.quality.metrics import MEASURE_SUM_PREFIX, NULL_RATE_PREFIX, UNKNOWN_KEY_RATE
from sentinel.review.base import ActionUnderReview, ReviewRequest

#: Above this share of invalid rows, row-level quarantine stops being "set the
#: bad rows aside" and becomes "throw the partition away".
MAX_QUARANTINE_SHARE = 0.5

#: How far ``factor x observed ratio`` may be from 1 for a unit correction to be
#: described as consistent with the anomaly.
FACTOR_TOLERANCE = 0.15

#: Actions that only contain: they change no data and are undone by a human
#: releasing a hold or closing a ticket.
CONTAINMENT_ACTIONS: frozenset[str] = frozenset(
    {"rerun_pipeline", "hold_downstream", "open_upstream_ticket"}
)


class RuleReviewer:
    """Approves an action only when the incident's evidence calls for it."""

    name = "rules"

    def review(self, request: ReviewRequest) -> list[ActionReview]:
        """Judge each action on its own against the incident's observations."""
        return [self._review_one(request.incident, action) for action in request.actions]

    # -- internals ---------------------------------------------------------

    def _review_one(self, incident: Incident, action: ActionUnderReview) -> ActionReview:
        dataset = action.arguments.get("dataset")
        if dataset not in (None, "") and str(dataset) != incident.dataset:
            return self._escalate(
                f"the evidence is about {incident.dataset}; nothing here speaks for {dataset}"
            )

        if action.name in CONTAINMENT_ACTIONS:
            return self._approve("containment only: changes no data and is easily undone")

        check = {
            "enable_dedupe": self._dedupe,
            "quarantine_invalid_rows": self._quarantine_rows,
            "add_column_mapping": self._column_mapping,
            "apply_unit_correction": self._unit_correction,
        }.get(action.name)
        if check is None:
            return self._escalate(f"no rule can vouch for {action.name}")
        return check(incident, action.arguments)

    def _dedupe(self, incident: Incident, arguments: dict[str, Any]) -> ActionReview:
        if "unique_natural_key" in _failed_checks(incident):
            return self._approve("unique_natural_key failed: the partition holds duplicate keys")
        return self._escalate("the uniqueness check passed; nothing shows duplicate keys")

    def _quarantine_rows(self, incident: Incident, arguments: dict[str, Any]) -> ActionReview:
        failed = _failed_checks(incident)
        row_level = sorted(
            name
            for name in failed
            if name.startswith("not_null_") or name == "referential_integrity_sku"
        )
        if not row_level:
            return self._escalate("no null or unknown-key check failed; no rows to set aside")
        if "schema_complete" in failed:
            return self._escalate(
                "a column is missing from the extract, so the nulls are likely a rename; "
                "quarantining would reject rows that a column mapping would recover"
            )
        share = _invalid_share(incident)
        if share > MAX_QUARANTINE_SHARE:
            return self._escalate(
                f"{share:.0%} of rows are invalid; quarantining them leaves too little "
                "of the partition to publish"
            )
        return self._approve(
            f"{', '.join(row_level)} failed on {share:.0%} of rows; the rest is usable"
        )

    def _column_mapping(self, incident: Incident, arguments: dict[str, Any]) -> ActionReview:
        canonical = str(arguments.get("canonical_column") or "")
        source = str(arguments.get("source_column") or "")
        present = [str(name) for name in incident.evidence.get("columns_in_extract") or []]
        expected = [str(name) for name in incident.evidence.get("expected_columns") or []]

        if "schema_complete" not in _failed_checks(incident):
            return self._escalate("the schema check passed; no column is missing")
        if canonical not in expected:
            return self._escalate(f"{canonical!r} is not a column this dataset expects")
        if canonical in present:
            return self._escalate(f"{canonical!r} is present in the extract; it was not renamed")
        if source not in present:
            return self._escalate(f"{source!r} is not in the extract")
        if source in expected:
            return self._escalate(
                f"{source!r} is already one of the dataset's own columns, not a renamed one"
            )
        return self._approve(
            f"{canonical!r} is missing from the extract and {source!r} is a new column in it"
        )

    def _unit_correction(self, incident: Incident, arguments: dict[str, Any]) -> ActionReview:
        column = str(arguments.get("column_name") or "")
        ratio = _anomaly_ratio(incident, f"{MEASURE_SUM_PREFIX}{column}")
        try:
            factor = float(arguments.get("factor") or 0.0)
        except (TypeError, ValueError):
            factor = 0.0

        if ratio is None:
            return self._escalate(f"no anomaly on the sum of {column!r}; nothing to correct")
        if abs(factor * ratio - 1.0) > FACTOR_TOLERANCE:
            return self._escalate(
                f"factor {factor:g} does not undo the observed {ratio:.2f}x shift in {column!r}"
            )
        return self._escalate(
            f"factor {factor:g} matches the observed {ratio:.2f}x shift in {column!r}, but a "
            "rule cannot tell whether every store shifted or only some; a human must confirm"
        )

    def _approve(self, reason: str) -> ActionReview:
        return ActionReview(verdict=ReviewVerdict.APPROVE, reason=reason, reviewer=self.name)

    def _escalate(self, reason: str) -> ActionReview:
        return ActionReview(verdict=ReviewVerdict.ESCALATE, reason=reason, reviewer=self.name)


def _failed_checks(incident: Incident) -> set[str]:
    return {str(check.get("check")) for check in incident.failed_checks}


def _invalid_share(incident: Incident) -> float:
    """The largest null or unknown-key rate recorded for the partition."""
    metrics = incident.evidence.get("metrics")
    if not isinstance(metrics, dict):
        return 1.0
    rates = [
        float(value)
        for name, value in metrics.items()
        if value is not None and (name.startswith(NULL_RATE_PREFIX) or name == UNKNOWN_KEY_RATE)
    ]
    return max(rates, default=1.0)


def _anomaly_ratio(incident: Incident, metric: str) -> float | None:
    for anomaly in incident.anomalies:
        if anomaly.get("metric") == metric and anomaly.get("ratio"):
            return float(anomaly["ratio"])
    return None
