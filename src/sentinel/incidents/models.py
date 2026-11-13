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


class ReviewVerdict(StrEnum):
    """What the reviewer concluded about one proposed action."""

    APPROVE = "approve"
    """The evidence supports the action; it may run without a human."""

    ESCALATE = "escalate"
    """A human should decide. Also the answer whenever the reviewer is unsure."""


class ActionReview(BaseModel):
    """An independent second opinion on one proposed action."""

    verdict: ReviewVerdict = ReviewVerdict.ESCALATE
    reason: str = ""
    reviewer: str = "unknown"
    """Who reviewed, e.g. ``rules`` or ``claude:claude-opus-5``."""


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
    review: ActionReview | None = None
    """The reviewer's opinion, when a reviewer is configured. Advisory for an
    action a hard rule already sent to a human; decisive for the rest."""

    hard_rules: list[str] = Field(default_factory=list)
    """Deterministic rules that require a human for this action, whatever the
    reviewer said."""


class AffectedAsset(BaseModel):
    """One downstream asset that takes data from the incident's dataset."""

    asset_id: str
    name: str
    kind: str
    """``table``, ``model``, ``report``, ``dashboard`` or ``decision``."""

    owner: str = ""
    criticality: str = "medium"
    distance: int = 1
    """Hops from the dataset: 1 reads it directly, more means through other assets."""

    via: list[str] = Field(default_factory=list)
    """The shortest path from the dataset to this asset, both ends included."""

    cost_per_hour: float = 0.0
    """What an hour of this asset running on wrong data is assumed to cost."""


class CostLine(BaseModel):
    """One term of the loss estimate, with the arithmetic behind it."""

    label: str
    amount: float
    basis: str = ""


class CostEstimate(BaseModel):
    """A rough monetary estimate of what an incident puts at stake.

    An estimate, never a measurement: every figure in it comes from the cost
    model's configured rates multiplied by counts, and ``assumptions`` says which.
    """

    currency: str = "USD"
    amount: float = 0.0
    low: float = 0.0
    high: float = 0.0
    exposure_hours: float = 0.0
    records_at_risk: int = 0
    components: list[CostLine] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class ImpactAssessment(BaseModel):
    """Blast radius and estimated loss, worked out when the incident opens."""

    dataset: str
    assets: list[AffectedAsset] = Field(default_factory=list)
    """Everything downstream of the dataset, nearest first."""

    cost: CostEstimate = Field(default_factory=CostEstimate)
    priority: str = "P3"
    """``P1`` (highest) to ``P3``, from the estimate against configured thresholds."""

    source: str = ""
    """Which lineage file the assessment was computed from."""

    assessed_at: datetime = Field(default_factory=utcnow)

    @property
    def consumers(self) -> list[AffectedAsset]:
        """Affected assets somebody reads or acts on - everything but tables."""
        return [asset for asset in self.assets if asset.kind != "table"]

    def count(self, kind: str) -> int:
        """How many affected assets are of one kind."""
        return sum(1 for asset in self.assets if asset.kind == kind)


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
    tools_used: list[str] = Field(default_factory=list)
    """Names of the investigation tools called, in order. Says *how* a conclusion
    was reached - in particular whether incident history was consulted."""

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

    impact: ImpactAssessment | None = None
    """Who downstream is affected and a rough loss estimate. ``None`` on incidents
    recorded before impact assessment existed, or when the lineage file is unusable."""

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
