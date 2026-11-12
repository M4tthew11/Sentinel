"""The incident document.

An incident is the unit of work the whole platform revolves around: detection
opens it, the agent diagnoses it, the executor acts on it, a human approves or
rejects, and the eval harness scores it. It is stored as one JSON document
because its shape is genuinely nested and genuinely evolving - exactly the case
where a document beats a wide relational table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    """Naive UTC timestamp, matching how DuckDB stores TIMESTAMP."""
    return datetime.now(tz=UTC).replace(tzinfo=None)


class RootCause(StrEnum):
    """The vocabulary the agent must answer in.

    Deliberately the same labels the fault library uses, which is what lets the
    eval harness score a diagnosis automatically instead of by hand.
    """

    VOLUME_DROP = "volume_drop"
    NULL_SPIKE = "null_spike"
    DUPLICATE_BATCH = "duplicate_batch"
    SCHEMA_DRIFT = "schema_drift"
    UNIT_OF_MEASURE = "unit_of_measure"
    LATE_ARRIVAL = "late_arrival"
    REFERENTIAL_BREAK = "referential_break"
    UNKNOWN = "unknown"


class IncidentStatus(StrEnum):
    """Lifecycle of an incident.

    ``REMEDIATED`` and ``RESOLVED`` are deliberately different states. Resolved
    means the data is correct again and every check passes. Remediated means the
    damage was contained - bad rows quarantined, publication held, a ticket
    raised - while the underlying feed is still wrong. Collapsing the two would
    let the platform claim it fixed a problem that only its owner can fix.
    """

    OPEN = "open"
    DIAGNOSED = "diagnosed"
    AWAITING_APPROVAL = "awaiting_approval"
    REMEDIATED = "remediated"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ActionStatus(StrEnum):
    """Outcome of a proposed remediation action."""

    PROPOSED = "proposed"
    EXECUTED = "executed"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    FAILED = "failed"


class RiskLevel(StrEnum):
    """How dangerous an action is, which decides whether it can auto-execute."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TimelineEvent(BaseModel):
    """One thing that happened to an incident."""

    at: datetime = Field(default_factory=utcnow)
    actor: str
    event: str
    detail: str = ""


class ProposedAction(BaseModel):
    """A remediation the agent wants to apply."""

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class ActionRecord(BaseModel):
    """A proposed action plus what actually happened to it."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    risk: RiskLevel = RiskLevel.LOW
    status: ActionStatus = ActionStatus.PROPOSED
    result: str = ""
    actor: str = ""
    at: datetime = Field(default_factory=utcnow)


class Diagnosis(BaseModel):
    """The agent's conclusion about an incident."""

    root_cause: RootCause = RootCause.UNKNOWN
    contributing_causes: list[RootCause] = Field(default_factory=list)
    """Further causes active on the same partition, beyond the primary one.

    Faults do not queue politely: a load can be duplicated *and* in the wrong
    unit. An engine that names only the first thing it finds fixes half the day.
    """

    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    reasoning: str = ""
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    engine: str = "unknown"
    """Which reasoning engine produced this, e.g. ``claude:claude-sonnet-5`` or ``rules``."""

    tool_calls: int = 0
    latency_ms: int = 0


class Incident(BaseModel):
    """A detected data problem and everything known about it."""

    model_config = ConfigDict(use_enum_values=False)

    incident_id: str = Field(default_factory=lambda: f"INC-{uuid.uuid4().hex[:10]}")
    run_id: str
    business_date: date
    dataset: str
    severity: str = "warning"
    status: IncidentStatus = IncidentStatus.OPEN
    detected_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    title: str = ""
    failed_checks: list[dict[str, Any]] = Field(default_factory=list)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)

    diagnosis: Diagnosis | None = None
    actions: list[ActionRecord] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)

    def log_event(self, actor: str, event: str, detail: str = "") -> None:
        """Append a timeline entry and bump ``updated_at``."""
        self.timeline.append(TimelineEvent(actor=actor, event=event, detail=detail))
        self.updated_at = utcnow()

    def to_json(self) -> str:
        """Serialise the document for storage."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, payload: str) -> Incident:
        """Rehydrate a document from storage."""
        return cls.model_validate_json(payload)
