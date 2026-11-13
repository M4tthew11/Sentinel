"""Learning from incident history: what counts as a precedent, and what the agent sees."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sentinel.agent.base import DiagnosisRequest
from sentinel.agent.claude_engine import ClaudeEngine
from sentinel.agent.prompts import SYSTEM_PROMPT
from sentinel.agent.tools import MAX_PRECEDENTS, TOOL_SCHEMAS, Toolbox
from sentinel.generator.faults import FaultSpec, FaultType
from sentinel.incidents.history import find_similar, has_outcome, signals
from sentinel.incidents.models import (
    ActionRecord,
    ActionStatus,
    Diagnosis,
    Incident,
    IncidentStatus,
    RootCause,
)
from sentinel.incidents.store import IncidentStore
from sentinel.monitor import monitor

NOW = datetime(2025, 3, 20, 9, 0, 0)

UNIT_SHIFT = [
    {"metric": "measure_avg__units_sold", "direction": "increase"},
    {"metric": "measure_sum__units_sold", "direction": "increase"},
]


def _incident(
    *,
    days_ago: int = 0,
    dataset: str = "pos_sales",
    status: IncidentStatus = IncidentStatus.OPEN,
    checks: tuple[str, ...] = (),
    anomalies: list[dict[str, Any]] | None = None,
    root_cause: RootCause | None = None,
    actions: list[ActionRecord] | None = None,
) -> Incident:
    detected = NOW - timedelta(days=days_ago)
    return Incident(
        run_id="RUN-test",
        business_date=detected.date(),
        dataset=dataset,
        status=status,
        detected_at=detected,
        title="test incident",
        failed_checks=[{"check": name} for name in checks],
        anomalies=anomalies or [],
        diagnosis=(
            None
            if root_cause is None
            else Diagnosis(root_cause=root_cause, confidence=0.8, summary="what happened")
        ),
        actions=actions or [],
    )


def _save(warehouse, *incidents: Incident) -> IncidentStore:
    store = IncidentStore(warehouse)
    for incident in incidents:
        store.save(incident)
    return store


# -- signature --------------------------------------------------------------


def test_signals_reduce_names_to_their_family() -> None:
    sales = _incident(checks=("unique_natural_key",), anomalies=UNIT_SHIFT)
    shipments = _incident(
        dataset="shipments",
        checks=("unique_natural_key",),
        anomalies=[
            {"metric": "measure_avg__cases_shipped", "direction": "increase"},
            {"metric": "measure_sum__cases_shipped", "direction": "increase"},
        ],
    )
    assert signals(sales) == signals(shipments)
    assert "anomaly:measure_avg:increase" in signals(sales)


def test_direction_is_part_of_the_signature() -> None:
    up = _incident(anomalies=[{"metric": "row_count", "direction": "increase"}])
    down = _incident(anomalies=[{"metric": "row_count", "direction": "decrease"}])
    assert not signals(up) & signals(down)


# -- what counts as a precedent ---------------------------------------------


def test_only_incidents_with_an_outcome_are_precedents(warehouse) -> None:
    resolved = _incident(
        days_ago=3,
        status=IncidentStatus.RESOLVED,
        checks=("unique_natural_key",),
        root_cause=RootCause.DUPLICATE_BATCH,
    )
    still_open = _incident(days_ago=2, checks=("unique_natural_key",))
    pending = _incident(
        days_ago=1,
        status=IncidentStatus.AWAITING_APPROVAL,
        checks=("unique_natural_key",),
        root_cause=RootCause.DUPLICATE_BATCH,
    )
    current = _incident(checks=("unique_natural_key",))
    store = _save(warehouse, resolved, still_open, pending, current)

    matches = find_similar(store, current)
    assert [match.incident.incident_id for match in matches] == [resolved.incident_id]
    assert matches[0].similarity == 1.0
    assert matches[0].shared_signals == ("check:unique_natural_key",)


def test_a_rejected_action_makes_an_unclosed_incident_a_precedent() -> None:
    rejected = _incident(
        status=IncidentStatus.DIAGNOSED,
        root_cause=RootCause.UNIT_OF_MEASURE,
        actions=[ActionRecord(name="apply_unit_correction", status=ActionStatus.REJECTED)],
    )
    untested = _incident(status=IncidentStatus.DIAGNOSED, root_cause=RootCause.UNIT_OF_MEASURE)
    assert has_outcome(rejected)
    assert not has_outcome(untested)


def test_lookup_never_returns_the_incident_itself_or_its_future(warehouse) -> None:
    current = _incident(
        days_ago=2,
        status=IncidentStatus.RESOLVED,
        checks=("schema_complete",),
        root_cause=RootCause.SCHEMA_DRIFT,
    )
    later = _incident(
        days_ago=0,
        status=IncidentStatus.RESOLVED,
        checks=("schema_complete",),
        root_cause=RootCause.SCHEMA_DRIFT,
    )
    store = _save(warehouse, current, later)
    assert find_similar(store, current) == []
    assert [m.incident.incident_id for m in find_similar(store, later)] == [current.incident_id]


def test_ranking_prefers_overlap_then_dataset_then_recency(warehouse) -> None:
    def closed(days_ago: int, **overrides: Any) -> Incident:
        payload: dict[str, Any] = {
            "days_ago": days_ago,
            "status": IncidentStatus.REMEDIATED,
            "anomalies": UNIT_SHIFT,
            "root_cause": RootCause.UNIT_OF_MEASURE,
        }
        payload.update(overrides)
        return _incident(**payload)

    older_exact = closed(9)
    newer_exact = closed(4)
    other_dataset = closed(1, dataset="shipments")
    partial = closed(2, anomalies=[*UNIT_SHIFT, {"metric": "row_count", "direction": "increase"}])
    unrelated = closed(3, anomalies=[{"metric": "row_count", "direction": "decrease"}])
    current = _incident(anomalies=UNIT_SHIFT)
    store = _save(warehouse, older_exact, newer_exact, other_dataset, partial, unrelated, current)

    matches = find_similar(store, current, limit=10)
    assert [match.incident.incident_id for match in matches] == [
        newer_exact.incident_id,
        older_exact.incident_id,
        other_dataset.incident_id,
        partial.incident_id,
    ]
    assert matches[2].similarity < 1.0

    same_dataset = find_similar(store, current, limit=10, same_dataset_only=True)
    assert other_dataset.incident_id not in [m.incident.incident_id for m in same_dataset]


def test_an_incident_with_no_signals_has_no_precedents(warehouse) -> None:
    past = _incident(days_ago=1, status=IncidentStatus.RESOLVED, root_cause=RootCause.UNKNOWN)
    current = _incident()
    assert find_similar(_save(warehouse, past, current), current) == []


# -- the tool ---------------------------------------------------------------


def test_tool_reports_how_the_precedent_ended(warehouse) -> None:
    past = _incident(
        days_ago=5,
        status=IncidentStatus.DIAGNOSED,
        anomalies=UNIT_SHIFT,
        root_cause=RootCause.UNIT_OF_MEASURE,
        actions=[
            ActionRecord(
                name="apply_unit_correction",
                arguments={"column_name": "units_sold", "factor": 12},
                status=ActionStatus.REJECTED,
                result="only the northern stores changed unit",
                actor="dana",
            )
        ],
    )
    past.log_event("sentinel-agent", "verification", "checks pass but metrics are still anomalous")
    current = _incident(anomalies=UNIT_SHIFT)
    _save(warehouse, past, current)

    toolbox = Toolbox(warehouse, current.business_date, incident=current)
    result = toolbox.call("find_similar_incidents", {})

    assert result["compared_on"] == sorted(signals(current))
    (precedent,) = result["precedents"]
    assert precedent["incident_id"] == past.incident_id
    assert precedent["diagnosis"]["root_cause"] == "unit_of_measure"
    assert "rejected" in precedent["outcome"]
    assert precedent["actions"] == [
        {
            "name": "apply_unit_correction",
            "arguments": {"column_name": "units_sold", "factor": 12},
            "status": "rejected",
            "result": "only the northern stores changed unit",
            "decided_by": "dana",
        }
    ]
    assert precedent["verification"].startswith("checks pass")
    assert toolbox.call_log == ["find_similar_incidents"]


def test_tool_says_so_when_nothing_matches(warehouse) -> None:
    current = _incident(checks=("partition_present",))
    _save(warehouse, current)
    result = Toolbox(warehouse, current.business_date, incident=current).call(
        "find_similar_incidents", {}
    )
    assert result["precedents"] == []
    assert "new" in result["note"]


def test_tool_caps_the_number_of_precedents(warehouse) -> None:
    past = [
        _incident(
            days_ago=offset,
            status=IncidentStatus.RESOLVED,
            checks=("unique_natural_key",),
            root_cause=RootCause.DUPLICATE_BATCH,
        )
        for offset in range(1, MAX_PRECEDENTS + 4)
    ]
    current = _incident(checks=("unique_natural_key",))
    _save(warehouse, *past, current)
    result = Toolbox(warehouse, current.business_date, incident=current).call(
        "find_similar_incidents", {"limit": 99}
    )
    assert len(result["precedents"]) == MAX_PRECEDENTS


def test_tool_needs_an_incident_to_compare_against(warehouse) -> None:
    result = Toolbox(warehouse, date(2025, 3, 1)).call("find_similar_incidents", {})
    assert "error" in result


def test_tool_is_offered_to_the_model_and_explained_in_the_prompt() -> None:
    assert "find_similar_incidents" in [schema["name"] for schema in TOOL_SCHEMAS]
    assert "find_similar_incidents" in SYSTEM_PROMPT
    assert "not evidence" in SYSTEM_PROMPT


# -- the engine -------------------------------------------------------------


class _Block:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Response:
    stop_reason = "tool_use"

    def __init__(self, *content: _Block) -> None:
        self.content = list(content)


class _Client:
    """Replays canned responses and records what the engine sent."""

    def __init__(self, *responses: _Response) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


def test_claude_engine_can_look_up_precedents(settings, warehouse) -> None:
    past = _incident(
        days_ago=7,
        status=IncidentStatus.REMEDIATED,
        anomalies=UNIT_SHIFT,
        root_cause=RootCause.UNIT_OF_MEASURE,
    )
    current = _incident(anomalies=UNIT_SHIFT)
    _save(warehouse, past, current)

    client = _Client(
        _Response(_Block(type="tool_use", id="toolu_0", name="find_similar_incidents", input={})),
        _Response(
            _Block(
                type="tool_use",
                id="toolu_1",
                name="submit_diagnosis",
                input={
                    "root_cause": "unit_of_measure",
                    "confidence": 0.7,
                    "summary": "Same shift as last week.",
                    "reasoning": f"Matches {past.incident_id}; confirmed on today's rows.",
                    "proposed_actions": [{"name": "hold_downstream"}],
                },
            )
        ),
    )
    engine = ClaudeEngine(settings.agent, client=client)  # type: ignore[arg-type]
    diagnosis = engine.diagnose(
        DiagnosisRequest(incident=current, warehouse=warehouse, settings=settings)
    )

    tool_result = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert past.incident_id in tool_result
    assert current.incident_id not in tool_result
    assert diagnosis.tools_used == ["find_similar_incidents"]
    assert diagnosis.tool_calls == 1


# -- end to end -------------------------------------------------------------


def test_a_recurring_fault_finds_its_first_occurrence(settings, seeded, next_day, inject) -> None:
    """History builds itself: the loop closes an incident, the next one can find it."""
    fault = FaultSpec("pos_sales", FaultType.VOLUME_DROP, magnitude=0.65)
    inject(next_day, fault)
    (first,) = monitor(next_day, seeded, settings, "test").incidents
    assert first.status is IncidentStatus.REMEDIATED

    repeat_day = next_day + timedelta(days=1)
    inject(repeat_day, fault)
    (second,) = monitor(repeat_day, seeded, settings, "test").incidents

    result = Toolbox(seeded, repeat_day, incident=second).call("find_similar_incidents", {})
    precedent = result["precedents"][0]
    assert precedent["incident_id"] == first.incident_id
    assert precedent["diagnosis"]["root_cause"] == "volume_drop"
    assert precedent["status"] == "remediated"
    assert {action["name"] for action in precedent["actions"]} >= {"hold_downstream"}
    assert all(action["status"] == "executed" for action in precedent["actions"])
