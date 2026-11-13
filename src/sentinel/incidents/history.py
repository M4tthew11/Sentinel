"""Precedent lookup: which closed incidents looked like this one, and how they ended.

Every incident the platform closes is a labelled example - the signals that
opened it, what the engine concluded, which actions ran, and whether verification
or a human agreed. This module turns that record into something an engine can
consult before it diagnoses a new incident.

Similarity is deliberately not an embedding. An incident's identity, for this
purpose, is the set of checks that failed and the metrics that moved, so two
incidents are alike when those sets overlap. That is cheap, deterministic, and
explainable: every match comes back with the exact signals it shares, so a
reader - human or model - can see why it was offered and judge whether it fits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sentinel.incidents.models import ActionStatus, Incident, IncidentStatus
from sentinel.incidents.store import IncidentStore

#: Matches below this overlap are noise rather than precedent.
MIN_SIMILARITY = 0.2

#: A precedent on another dataset is still informative, but less so: the feeds
#: have different owners, shapes and failure habits.
CROSS_DATASET_WEIGHT = 0.8

#: How many past incidents are scored per lookup, newest first.
MAX_CANDIDATES = 500

MAX_TEXT_CHARS = 300

_OUTCOMES = {
    IncidentStatus.RESOLVED: "fixed and verified: the partition was healthy after the actions ran",
    IncidentStatus.REMEDIATED: (
        "contained, not fixed: the actions ran but the underlying feed was still wrong"
    ),
    IncidentStatus.DISMISSED: "dismissed by a human as not a real problem",
}
_REJECTED_OUTCOME = "a human rejected a proposed action - read the reason before repeating it"


@dataclass(frozen=True)
class Precedent:
    """One past incident, how alike it is, and why."""

    incident: Incident
    similarity: float
    shared_signals: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """The compact form handed to the agent."""
        incident = self.incident
        diagnosis = incident.diagnosis
        assert diagnosis is not None
        verification = [
            event.detail for event in incident.timeline if event.event == "verification"
        ]
        return {
            "incident_id": incident.incident_id,
            "dataset": incident.dataset,
            "business_date": incident.business_date.isoformat(),
            "similarity": round(self.similarity, 2),
            "shared_signals": list(self.shared_signals),
            "status": str(incident.status),
            "outcome": outcome(incident),
            "diagnosis": {
                "root_cause": str(diagnosis.root_cause),
                "contributing_causes": [str(cause) for cause in diagnosis.contributing_causes],
                "confidence": diagnosis.confidence,
                "summary": _clip(diagnosis.summary),
                "engine": diagnosis.engine,
            },
            "actions": [
                {
                    "name": action.name,
                    "arguments": action.arguments,
                    "status": str(action.status),
                    "result": _clip(action.result),
                    "decided_by": action.actor,
                }
                for action in incident.actions
            ],
            "verification": _clip(verification[-1]) if verification else None,
        }


def signals(incident: Incident) -> frozenset[str]:
    """The signature of an incident: what failed and what moved, and which way.

    Metric and check names are reduced to their family (``measure_avg__units_sold``
    becomes ``measure_avg``) so that a duplicated shipments file and a duplicated
    sales file share a signature even though their measure columns differ.
    """
    found: set[str] = set()
    for check in incident.failed_checks:
        name = str(check.get("check") or "")
        if name:
            found.add(f"check:{_family(name)}")
    for anomaly in incident.anomalies:
        metric = str(anomaly.get("metric") or "")
        if metric:
            found.add(f"anomaly:{_family(metric)}:{anomaly.get('direction') or '?'}")
    return frozenset(found)


def has_outcome(incident: Incident) -> bool:
    """Whether an incident ended in a way that teaches something.

    An incident that is still open, or was diagnosed and then left alone, is an
    opinion without a result. A closed one has been tested - by verification or
    by a human - and so has one whose proposed action a human turned down.
    """
    if incident.diagnosis is None:
        return False
    if incident.status in _OUTCOMES:
        return True
    return any(action.status is ActionStatus.REJECTED for action in incident.actions)


def outcome(incident: Incident) -> str:
    """One line on how a past incident ended."""
    if any(action.status is ActionStatus.REJECTED for action in incident.actions):
        base = _OUTCOMES.get(incident.status)
        return f"{_REJECTED_OUTCOME}; {base}" if base else _REJECTED_OUTCOME
    return _OUTCOMES.get(incident.status, str(incident.status))


def find_similar(
    store: IncidentStore,
    incident: Incident,
    limit: int = 3,
    same_dataset_only: bool = False,
) -> list[Precedent]:
    """Closed incidents detected before ``incident`` that share its signals.

    Ordered by similarity, then by recency. Only incidents detected strictly
    earlier are considered, so a lookup can never return the incident itself or
    anything from its future - which also keeps a replayed eval honest.
    """
    target = signals(incident)
    if not target:
        return []

    matches: list[Precedent] = []
    for candidate in store.detected_before(incident, limit=MAX_CANDIDATES):
        if not has_outcome(candidate):
            continue
        if same_dataset_only and candidate.dataset != incident.dataset:
            continue
        other = signals(candidate)
        shared = target & other
        if not shared:
            continue
        similarity = len(shared) / len(target | other)
        if candidate.dataset != incident.dataset:
            similarity *= CROSS_DATASET_WEIGHT
        if similarity < MIN_SIMILARITY:
            continue
        matches.append(Precedent(candidate, similarity, tuple(sorted(shared))))

    # Stable sort: candidates arrive newest first, so ties stay newest first.
    matches.sort(key=lambda match: match.similarity, reverse=True)
    return matches[: max(0, limit)]


def _family(name: str) -> str:
    return name.split("__", 1)[0]


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT_CHARS else text[: MAX_TEXT_CHARS - 3] + "..."
