"""The deterministic diagnosis engine.

A decision tree over the same evidence the LLM sees. It exists for three reasons:

* the demo, the tests and CI must run with no API key and no network;
* it is the baseline the LLM is measured against in ``sentinel.evals`` - a model
  that cannot beat it is not worth the latency or the cost;
* when the LLM is unavailable or times out, production still self-heals.

It is deliberately conservative: where the evidence is ambiguous it answers
``unknown`` and proposes a ticket rather than guessing at a data mutation.
"""

from __future__ import annotations

import difflib
import time
from typing import Any

from sentinel.agent.base import DiagnosisRequest
from sentinel.agent.tools import Toolbox
from sentinel.incidents.models import Diagnosis, ProposedAction, RootCause
from sentinel.logging import get_logger
from sentinel.quality import metrics as m

log = get_logger(__name__)

#: A measure whose average moves by more than this factor, on a normal row count,
#: is treated as a unit change rather than a demand swing.
UNIT_CHANGE_RATIO = 1.6

#: Row count within this band of expectation counts as "normal volume".
NORMAL_VOLUME_BAND = (0.85, 1.15)


class RuleBasedEngine:
    """Diagnoses incidents with explicit rules instead of a model."""

    name = "rules"

    def diagnose(self, request: DiagnosisRequest) -> Diagnosis:
        """Classify the incident and propose actions."""
        started = time.perf_counter()
        incident = request.incident
        toolbox = Toolbox(request.warehouse, incident.business_date)
        checks = {check.get("check"): check for check in incident.failed_checks}

        diagnosis = (
            self._late_arrival(incident, checks)
            or self._schema_drift(incident, checks, toolbox)
            or self._duplicate_batch(incident, checks)
            or self._null_spike(incident, checks)
            or self._referential_break(incident, checks)
            or self._unit_of_measure(incident)
            or self._volume_drop(incident)
            or self._unknown(incident)
        )
        diagnosis.engine = self.name
        diagnosis.tool_calls = len(toolbox.call_log)
        diagnosis.latency_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "rules.diagnosed",
            incident_id=incident.incident_id,
            root_cause=str(diagnosis.root_cause),
            confidence=diagnosis.confidence,
        )
        return diagnosis

    # -- individual rules --------------------------------------------------

    def _late_arrival(self, incident: Any, checks: dict[Any, Any]) -> Diagnosis | None:
        if "partition_present" not in checks:
            return None
        return Diagnosis(
            root_cause=RootCause.LATE_ARRIVAL,
            confidence=0.95,
            summary=(
                f"No {incident.dataset} extract arrived for {incident.business_date.isoformat()}."
            ),
            reasoning=(
                "The partition_present check failed, which means the bronze layer holds "
                "zero rows for the business date. Nothing downstream can be corrected "
                "inside the warehouse until the file is delivered."
            ),
            proposed_actions=[
                ProposedAction(
                    name="hold_downstream",
                    arguments={
                        "dataset": incident.dataset,
                        "reason": (
                            f"{incident.dataset} partition missing for "
                            f"{incident.business_date.isoformat()}"
                        ),
                    },
                    rationale="Publishing gold without this feed would understate the day.",
                ),
                ProposedAction(
                    name="open_upstream_ticket",
                    arguments={
                        "dataset": incident.dataset,
                        "summary": (
                            f"Missing {incident.dataset} extract for "
                            f"{incident.business_date.isoformat()}"
                        ),
                        "body": "The scheduled extract never landed. Please redeliver.",
                    },
                    rationale="Only the source system can supply the missing file.",
                ),
            ],
        )

    def _schema_drift(
        self, incident: Any, checks: dict[Any, Any], toolbox: Toolbox
    ) -> Diagnosis | None:
        if "schema_complete" not in checks:
            return None
        # Routed through call() rather than invoked directly, so the tool shows up
        # in the eval report's tool-call count for both engines alike.
        columns = toolbox.call("get_partition_columns", {"dataset": incident.dataset})
        unresolved = list(columns["unresolved_columns"])
        present = list(columns["columns_in_extract"])
        mapping: dict[str, list[str]] = dict(columns["current_mapping"])
        known = {source for candidates in mapping.values() for source in candidates}
        unused = [name for name in present if name not in known]

        actions: list[ProposedAction] = []
        matches: list[str] = []
        for canonical in unresolved:
            candidate = _best_match(canonical, unused)
            if candidate is None:
                continue
            matches.append(f"{canonical} -> {candidate}")
            actions.append(
                ProposedAction(
                    name="add_column_mapping",
                    arguments={
                        "dataset": incident.dataset,
                        "canonical_column": canonical,
                        "source_column": candidate,
                    },
                    rationale=(
                        f"{candidate!r} is present in the extract, unused by any mapping, "
                        f"and is the closest name to {canonical!r}."
                    ),
                )
            )

        if not actions:
            return Diagnosis(
                root_cause=RootCause.SCHEMA_DRIFT,
                confidence=0.6,
                summary=(
                    f"Columns {unresolved} are missing from the {incident.dataset} extract "
                    "and no replacement column is recognisable."
                ),
                reasoning=(
                    f"Extract columns: {present}. Expected: {list(mapping)}. "
                    "No unused column resembles the missing ones, so a mapping would be a guess."
                ),
                proposed_actions=[
                    ProposedAction(
                        name="hold_downstream",
                        arguments={
                            "dataset": incident.dataset,
                            "reason": f"unmapped columns {unresolved}",
                        },
                        rationale="The silver contract cannot be satisfied.",
                    ),
                    ProposedAction(
                        name="open_upstream_ticket",
                        arguments={
                            "dataset": incident.dataset,
                            "summary": f"Schema change in {incident.dataset}: {unresolved} absent",
                            "body": f"Columns delivered: {present}",
                        },
                        rationale="The source owner must confirm the new contract.",
                    ),
                ],
            )

        actions.append(
            ProposedAction(
                name="rerun_pipeline",
                arguments={"business_date": incident.business_date.isoformat()},
                rationale="Rebuild silver and gold with the corrected mapping.",
            )
        )
        return Diagnosis(
            root_cause=RootCause.SCHEMA_DRIFT,
            confidence=0.9,
            summary=(
                f"The {incident.dataset} source renamed {', '.join(matches)}; "
                "the silver projection no longer resolves."
            ),
            reasoning=(
                f"Expected columns {unresolved} are absent from the extract, while "
                f"{[a.arguments['source_column'] for a in actions[:-1]]} are present and unused. "
                "Row count is unaffected, so this is a naming change rather than data loss."
            ),
            proposed_actions=actions,
        )

    def _duplicate_batch(self, incident: Any, checks: dict[Any, Any]) -> Diagnosis | None:
        if "unique_natural_key" not in checks:
            return None
        observed = checks["unique_natural_key"].get("observed")
        return Diagnosis(
            root_cause=RootCause.DUPLICATE_BATCH,
            confidence=0.9,
            summary=(
                f"{incident.dataset} contains duplicate natural keys (duplicate rate {observed})."
            ),
            reasoning=(
                "The natural key is not unique in this partition, which is the signature of "
                "the same batch being delivered or loaded twice. Silver rebuilds from bronze, "
                "so deduplicating on the key restores correct measures without deleting data."
            ),
            proposed_actions=[
                ProposedAction(
                    name="enable_dedupe",
                    arguments={"dataset": incident.dataset},
                    rationale="Keep the latest row per natural key on every rebuild.",
                ),
                ProposedAction(
                    name="rerun_pipeline",
                    arguments={"business_date": incident.business_date.isoformat()},
                    rationale="Republish the corrected figures.",
                ),
            ],
        )

    def _null_spike(self, incident: Any, checks: dict[Any, Any]) -> Diagnosis | None:
        failed = [name for name in checks if isinstance(name, str) and name.startswith("not_null_")]
        if not failed:
            return None
        columns = [name.removeprefix("not_null_") for name in failed]
        observed = max(float(checks[name].get("observed") or 0.0) for name in failed)
        return Diagnosis(
            root_cause=RootCause.NULL_SPIKE,
            confidence=0.85,
            summary=(
                f"Mandatory column(s) {', '.join(columns)} are null on "
                f"{observed:.1%} of {incident.dataset} rows."
            ),
            reasoning=(
                "Nulls appear in columns declared mandatory by the contract, while the schema "
                "itself resolved correctly. That points at an upstream join or mapping losing "
                "values, not at a structural change on our side."
            ),
            proposed_actions=[
                ProposedAction(
                    name="quarantine_invalid_rows",
                    arguments={"dataset": incident.dataset},
                    rationale="Keep the usable rows in silver, isolate the broken ones.",
                ),
                ProposedAction(
                    name="open_upstream_ticket",
                    arguments={
                        "dataset": incident.dataset,
                        "summary": f"Null {', '.join(columns)} in {incident.dataset}",
                        "body": f"{observed:.1%} of rows on {incident.business_date} are null.",
                    },
                    rationale="Only the source can restore the missing values.",
                ),
                ProposedAction(
                    name="rerun_pipeline",
                    arguments={"business_date": incident.business_date.isoformat()},
                    rationale="Rebuild silver with the invalid rows quarantined.",
                ),
            ],
        )

    def _referential_break(self, incident: Any, checks: dict[Any, Any]) -> Diagnosis | None:
        if "referential_integrity_sku" not in checks:
            return None
        observed = float(checks["referential_integrity_sku"].get("observed") or 0.0)
        return Diagnosis(
            root_cause=RootCause.REFERENTIAL_BREAK,
            confidence=0.85,
            summary=(
                f"{observed:.1%} of {incident.dataset} rows reference SKUs missing from "
                "the product master."
            ),
            reasoning=(
                "Keys that do not exist in dim_sku cannot be attributed to a brand, so they "
                "would silently distort brand-level gold tables. Quarantining keeps the "
                "attributable rows correct while the master data catches up."
            ),
            proposed_actions=[
                ProposedAction(
                    name="quarantine_invalid_rows",
                    arguments={"dataset": incident.dataset},
                    rationale="Prevent unattributable rows from reaching gold.",
                ),
                ProposedAction(
                    name="open_upstream_ticket",
                    arguments={
                        "dataset": incident.dataset,
                        "summary": f"Unknown SKUs in {incident.dataset}",
                        "body": "Product master is out of sync with the transactional feed.",
                    },
                    rationale="Master data must be synchronised at the source.",
                ),
                ProposedAction(
                    name="rerun_pipeline",
                    arguments={"business_date": incident.business_date.isoformat()},
                    rationale="Rebuild silver without the orphan rows.",
                ),
            ],
        )

    def _unit_of_measure(self, incident: Any) -> Diagnosis | None:
        signals = {str(signal["metric"]): signal for signal in incident.anomalies}
        row_signal = signals.get(m.ROW_COUNT)
        row_ratio = float(row_signal["ratio"]) if row_signal else 1.0
        if not NORMAL_VOLUME_BAND[0] <= row_ratio <= NORMAL_VOLUME_BAND[1]:
            return None

        candidates = [
            signal
            for metric, signal in signals.items()
            if metric.startswith(m.MEASURE_AVG_PREFIX)
            and float(signal["ratio"]) >= UNIT_CHANGE_RATIO
        ]
        if not candidates:
            return None

        signal = max(candidates, key=lambda item: float(item["ratio"]))
        column = str(signal["metric"]).removeprefix(m.MEASURE_AVG_PREFIX)
        ratio = float(signal["ratio"])
        factor = round(1.0 / ratio, 6)
        return Diagnosis(
            root_cause=RootCause.UNIT_OF_MEASURE,
            confidence=0.7,
            summary=(
                f"{incident.dataset}.{column} is {ratio:.2f}x its usual level on an unchanged "
                "row count, consistent with a change of reporting unit."
            ),
            reasoning=(
                f"Row count is within {NORMAL_VOLUME_BAND} of expectation "
                f"(ratio {row_ratio:.2f}), so no extra rows arrived, yet the per-row average "
                f"of {column} rose {ratio:.2f}x. A demand swing moves rows and values "
                "together; a unit change moves only the value."
            ),
            proposed_actions=[
                ProposedAction(
                    name="hold_downstream",
                    arguments={
                        "dataset": incident.dataset,
                        "reason": f"suspected unit change in {column}",
                    },
                    rationale="Do not publish inflated business figures while unconfirmed.",
                ),
                ProposedAction(
                    name="apply_unit_correction",
                    arguments={
                        "dataset": incident.dataset,
                        "column_name": column,
                        "factor": factor,
                        "business_date": incident.business_date.isoformat(),
                    },
                    rationale=(
                        f"Undo the observed {ratio:.2f}x inflation. Rewrites reported figures, "
                        "so it needs human approval."
                    ),
                ),
                ProposedAction(
                    name="open_upstream_ticket",
                    arguments={
                        "dataset": incident.dataset,
                        "summary": f"Suspected unit change in {incident.dataset}.{column}",
                        "body": f"Observed factor {ratio:.2f} against the weekday baseline.",
                    },
                    rationale="Confirm the new unit with the data owner before trusting it.",
                ),
            ],
        )

    def _volume_drop(self, incident: Any) -> Diagnosis | None:
        signals = {str(signal["metric"]): signal for signal in incident.anomalies}
        row_signal = signals.get(m.ROW_COUNT)
        if row_signal is None or float(row_signal["ratio"]) >= NORMAL_VOLUME_BAND[0]:
            return None
        ratio = float(row_signal["ratio"])
        return Diagnosis(
            root_cause=RootCause.VOLUME_DROP,
            confidence=0.8,
            summary=(
                f"{incident.dataset} delivered {ratio:.0%} of the rows expected for this weekday."
            ),
            reasoning=(
                f"Row count is {row_signal['observed']:.0f} against an expected "
                f"{row_signal['expected']:.0f} for the same weekday, and every structural "
                "check passed - the rows that did arrive are well formed. That is a partial "
                "delivery, not corruption."
            ),
            proposed_actions=[
                ProposedAction(
                    name="hold_downstream",
                    arguments={
                        "dataset": incident.dataset,
                        "reason": f"partial delivery ({ratio:.0%} of expected rows)",
                    },
                    rationale="An incomplete day would read as a demand collapse in gold.",
                ),
                ProposedAction(
                    name="open_upstream_ticket",
                    arguments={
                        "dataset": incident.dataset,
                        "summary": (
                            f"Partial {incident.dataset} extract on "
                            f"{incident.business_date.isoformat()}"
                        ),
                        "body": (
                            f"Received {row_signal['observed']:.0f} rows, expected about "
                            f"{row_signal['expected']:.0f}. Please redeliver the full extract."
                        ),
                    },
                    rationale="The missing rows only exist upstream.",
                ),
            ],
        )

    def _unknown(self, incident: Any) -> Diagnosis:
        return Diagnosis(
            root_cause=RootCause.UNKNOWN,
            confidence=0.3,
            summary=f"{incident.dataset} deviates from its baseline for reasons not yet explained.",
            reasoning=(
                "No structural check failed and no rule matched the anomaly pattern. Guessing "
                "at a data mutation here would be worse than escalating."
            ),
            proposed_actions=[
                ProposedAction(
                    name="open_upstream_ticket",
                    arguments={
                        "dataset": incident.dataset,
                        "summary": f"Unexplained anomaly in {incident.dataset}",
                        "body": "Automated triage could not attribute a root cause.",
                    },
                    rationale="Escalate to a human rather than act on a guess.",
                )
            ],
        )


def _best_match(canonical: str, candidates: list[str]) -> str | None:
    """Find the extract column most likely to be the renamed ``canonical`` column."""
    if not candidates:
        return None
    prefixed = [name for name in candidates if name.startswith(canonical)]
    if prefixed:
        return min(prefixed, key=len)
    close = difflib.get_close_matches(canonical, candidates, n=1, cutoff=0.6)
    return close[0] if close else None
