"""The remediation whitelist.

This module is the security boundary of the whole project. An LLM proposes
*names and arguments*; it never proposes SQL, shell commands or code. Anything
outside this registry is rejected before it reaches the warehouse, and each entry
carries a risk level that decides whether it may auto-execute or must wait for a
human.

Every action is also reversible or additive by construction: they write
configuration rows, never destructive DML against silver or gold. Silver is a
full rebuild from bronze, so "undo" is "delete the config row and rerun".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sentinel.config import Settings
from sentinel.elt import ops
from sentinel.elt.schema import COLUMN_TYPES
from sentinel.elt.warehouse import Warehouse
from sentinel.incidents.models import Incident, RiskLevel
from sentinel.logging import get_logger

log = get_logger(__name__)


@dataclass
class ActionContext:
    """Everything an action handler is allowed to touch."""

    warehouse: Warehouse
    incident: Incident
    settings: Settings
    actor: str


Handler = Callable[[ActionContext, dict[str, Any]], str]


@dataclass(frozen=True)
class ActionDefinition:
    """One whitelisted remediation."""

    name: str
    description: str
    risk: RiskLevel
    parameters: dict[str, dict[str, Any]]
    """Parameter name -> JSON-schema fragment, used both to validate calls and to
    describe the action to the model."""

    required: tuple[str, ...]
    handler: Handler
    triggers_rebuild: bool = True
    """Whether applying this action changes what a silver rebuild would produce."""

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Reject unknown or missing arguments before anything is executed."""
        unknown = set(arguments) - set(self.parameters)
        if unknown:
            raise ValueError(f"unknown argument(s) for {self.name}: {sorted(unknown)}")
        missing = [name for name in self.required if arguments.get(name) in (None, "")]
        if missing:
            raise ValueError(f"missing argument(s) for {self.name}: {missing}")
        return arguments


# --------------------------------------------------------------------------
# Argument coercion helpers
# --------------------------------------------------------------------------


def _dataset_arg(context: ActionContext, arguments: dict[str, Any]) -> str:
    """Resolve the dataset an action targets, defaulting to the incident's."""
    dataset = str(arguments.get("dataset") or context.incident.dataset)
    if dataset not in COLUMN_TYPES:
        raise ValueError(f"unknown dataset: {dataset!r}")
    return dataset


def _date_arg(context: ActionContext, arguments: dict[str, Any], key: str) -> date:
    """Resolve a date argument, defaulting to the incident's business date."""
    raw = arguments.get(key)
    if raw in (None, ""):
        return context.incident.business_date
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw))


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def _rerun_pipeline(context: ActionContext, arguments: dict[str, Any]) -> str:
    """No-op marker: the executor always reruns once after applying changes."""
    business_date = _date_arg(context, arguments, "business_date")
    return f"pipeline rerun scheduled for {business_date.isoformat()}"


def _enable_dedupe(context: ActionContext, arguments: dict[str, Any]) -> str:
    dataset = _dataset_arg(context, arguments)
    ops.set_flag(context.warehouse, dataset, "dedupe", "true", context.actor)
    return f"deduplication on the natural key enabled for {dataset}"


def _quarantine_invalid_rows(context: ActionContext, arguments: dict[str, Any]) -> str:
    dataset = _dataset_arg(context, arguments)
    ops.set_flag(context.warehouse, dataset, "quarantine_invalid_rows", "true", context.actor)
    return (
        f"invalid rows for {dataset} will be routed to silver_{dataset}_rejected "
        "instead of entering the silver table"
    )


def _quarantine_partition(context: ActionContext, arguments: dict[str, Any]) -> str:
    dataset = _dataset_arg(context, arguments)
    business_date = _date_arg(context, arguments, "business_date")
    reason = str(arguments.get("reason") or context.incident.title or "quarantined by agent")
    ops.quarantine_partition(context.warehouse, dataset, business_date, reason, context.actor)
    return f"partition {dataset}/{business_date.isoformat()} excluded from silver"


def _add_column_mapping(context: ActionContext, arguments: dict[str, Any]) -> str:
    dataset = _dataset_arg(context, arguments)
    canonical = str(arguments["canonical_column"])
    source = str(arguments["source_column"])
    ops.set_column_mapping(context.warehouse, dataset, canonical, source, context.actor)
    return f"{dataset}.{canonical} now reads from source column {source!r}"


def _hold_downstream(context: ActionContext, arguments: dict[str, Any]) -> str:
    dataset = arguments.get("dataset")
    business_date = _date_arg(context, arguments, "business_date")
    reason = str(arguments.get("reason") or context.incident.title or "held by agent")
    hold_id = ops.create_hold(
        context.warehouse,
        None if dataset is None else str(dataset),
        business_date,
        reason,
        context.actor,
    )
    return f"gold publication held for {business_date.isoformat()} ({hold_id}): {reason}"


def _open_upstream_ticket(context: ActionContext, arguments: dict[str, Any]) -> str:
    dataset = _dataset_arg(context, arguments)
    summary = str(arguments["summary"])
    body = str(arguments.get("body") or "")
    ticket_id = ops.create_ticket(context.warehouse, dataset, summary, body, context.actor)
    return f"ticket {ticket_id} raised with the upstream data owner"


def _apply_unit_correction(context: ActionContext, arguments: dict[str, Any]) -> str:
    dataset = _dataset_arg(context, arguments)
    column = str(arguments["column_name"])
    factor = float(arguments["factor"])
    business_date = _date_arg(context, arguments, "business_date")
    correction_id = ops.add_correction(
        context.warehouse, dataset, column, factor, business_date, context.actor
    )
    return (
        f"correction {correction_id}: {dataset}.{column} multiplied by {factor:g} "
        f"for {business_date.isoformat()}"
    )


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_DATASET_PARAM = {
    "type": "string",
    "enum": sorted(COLUMN_TYPES),
    "description": "Dataset to act on. Defaults to the incident's dataset.",
}
_DATE_PARAM = {
    "type": "string",
    "description": "Business date as YYYY-MM-DD. Defaults to the incident's business date.",
}

ACTIONS: dict[str, ActionDefinition] = {
    "rerun_pipeline": ActionDefinition(
        name="rerun_pipeline",
        description=(
            "Rebuild silver and gold for the business date. Use after a configuration "
            "change so the corrected data is republished."
        ),
        risk=RiskLevel.LOW,
        parameters={"business_date": _DATE_PARAM},
        required=(),
        handler=_rerun_pipeline,
    ),
    "enable_dedupe": ActionDefinition(
        name="enable_dedupe",
        description=(
            "Keep only the latest row per natural key when rebuilding silver. Use when "
            "the same batch was delivered or loaded twice."
        ),
        risk=RiskLevel.LOW,
        parameters={"dataset": _DATASET_PARAM},
        required=(),
        handler=_enable_dedupe,
    ),
    "quarantine_invalid_rows": ActionDefinition(
        name="quarantine_invalid_rows",
        description=(
            "Route rows failing the contract (null mandatory field, unknown SKU) to the "
            "rejected table instead of letting them into silver. Use when part of a "
            "partition is corrupt but the rest is usable."
        ),
        risk=RiskLevel.LOW,
        parameters={"dataset": _DATASET_PARAM},
        required=(),
        handler=_quarantine_invalid_rows,
    ),
    "quarantine_partition": ActionDefinition(
        name="quarantine_partition",
        description=(
            "Exclude an entire business date of one dataset from silver. Use when the "
            "whole partition is untrustworthy."
        ),
        risk=RiskLevel.MEDIUM,
        parameters={
            "dataset": _DATASET_PARAM,
            "business_date": _DATE_PARAM,
            "reason": {"type": "string", "description": "Why the partition is untrustworthy."},
        },
        required=(),
        handler=_quarantine_partition,
    ),
    "add_column_mapping": ActionDefinition(
        name="add_column_mapping",
        description=(
            "Map a canonical silver column onto a differently named source column. Use "
            "when an upstream system renamed a field."
        ),
        risk=RiskLevel.MEDIUM,
        parameters={
            "dataset": _DATASET_PARAM,
            "canonical_column": {
                "type": "string",
                "description": "The canonical column name the warehouse expects.",
            },
            "source_column": {
                "type": "string",
                "description": "The column name actually present in the extract.",
            },
        },
        required=("canonical_column", "source_column"),
        handler=_add_column_mapping,
    ),
    "hold_downstream": ActionDefinition(
        name="hold_downstream",
        description=(
            "Block publication of the gold layer for this business date until a human "
            "releases the hold. Use when the data is incomplete and publishing it would "
            "mislead the business. Other business dates keep publishing normally."
        ),
        risk=RiskLevel.LOW,
        parameters={
            "dataset": _DATASET_PARAM,
            "business_date": _DATE_PARAM,
            "reason": {"type": "string", "description": "Why downstream must not publish."},
        },
        required=("reason",),
        handler=_hold_downstream,
        triggers_rebuild=False,
    ),
    "open_upstream_ticket": ActionDefinition(
        name="open_upstream_ticket",
        description=(
            "Raise a ticket with the owner of the source system. Use whenever the root "
            "cause is upstream and cannot be fixed inside the warehouse."
        ),
        risk=RiskLevel.LOW,
        parameters={
            "dataset": _DATASET_PARAM,
            "summary": {"type": "string", "description": "One-line summary for the data owner."},
            "body": {"type": "string", "description": "Evidence and the requested fix."},
        },
        required=("summary",),
        handler=_open_upstream_ticket,
        triggers_rebuild=False,
    ),
    "apply_unit_correction": ActionDefinition(
        name="apply_unit_correction",
        description=(
            "Multiply a measure by a constant factor when the source changed unit "
            "(for example eaches to cases). This rewrites reported business numbers, so "
            "it always requires human approval."
        ),
        risk=RiskLevel.HIGH,
        parameters={
            "dataset": _DATASET_PARAM,
            "column_name": {"type": "string", "description": "Measure column to correct."},
            "factor": {
                "type": "number",
                "description": "Multiplier applied to the measure, e.g. 0.0833 to undo a x12.",
            },
            "business_date": _DATE_PARAM,
        },
        required=("column_name", "factor"),
        handler=_apply_unit_correction,
    ),
}


def action_catalogue() -> str:
    """Render the whitelist as text for the agent prompt."""
    lines: list[str] = []
    for action in ACTIONS.values():
        params = ", ".join(
            f"{name}{'' if name in action.required else '?'}" for name in action.parameters
        )
        lines.append(f"- {action.name}({params}) [risk={action.risk}] - {action.description}")
    return "\n".join(lines)
