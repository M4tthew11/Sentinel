"""Engine behaviour: the decision tree, the investigation tools, and the fallback."""

from __future__ import annotations

from datetime import date
from typing import Any

import anthropic
import httpx
import pytest

from sentinel.agent.base import DiagnosisRequest, build_engine
from sentinel.agent.claude_engine import ClaudeEngine
from sentinel.agent.rules import RuleBasedEngine
from sentinel.agent.tools import Toolbox
from sentinel.config import AgentProvider, Settings
from sentinel.generator.faults import FaultSpec, FaultType
from sentinel.incidents.models import Incident, RootCause
from sentinel.monitor import monitor
from sentinel.runner import refresh_run


def _incident(dataset: str = "pos_sales", **overrides: Any) -> Incident:
    payload: dict[str, Any] = {
        "run_id": "RUN-test",
        "business_date": date(2025, 3, 17),
        "dataset": dataset,
        "severity": "critical",
        "title": "test incident",
    }
    payload.update(overrides)
    return Incident(**payload)


def _check(name: str, observed: float = 1.0) -> dict[str, Any]:
    return {
        "dataset": "pos_sales",
        "check": name,
        "severity": "critical",
        "passed": False,
        "observed": observed,
        "threshold": 0.0,
        "details": "failed",
    }


def _signal(metric: str, observed: float, expected: float) -> dict[str, Any]:
    return {
        "metric": metric,
        "observed": observed,
        "expected": expected,
        "ratio": observed / expected,
        "zscore": 9.0,
        "direction": "increase" if observed > expected else "decrease",
    }


def _diagnose(incident: Incident, warehouse, settings: Settings):
    return RuleBasedEngine().diagnose(
        DiagnosisRequest(incident=incident, warehouse=warehouse, settings=settings)
    )


# -- the decision tree -----------------------------------------------------


def test_missing_partition_reads_as_late_arrival(warehouse, settings) -> None:
    incident = _incident(failed_checks=[_check("partition_present")])
    diagnosis = _diagnose(incident, warehouse, settings)
    assert diagnosis.root_cause is RootCause.LATE_ARRIVAL
    assert [action.name for action in diagnosis.proposed_actions] == [
        "hold_downstream",
        "open_upstream_ticket",
    ]


def test_duplicate_keys_read_as_a_double_load(warehouse, settings) -> None:
    incident = _incident(failed_checks=[_check("unique_natural_key", 0.33)])
    diagnosis = _diagnose(incident, warehouse, settings)
    assert diagnosis.root_cause is RootCause.DUPLICATE_BATCH
    assert "enable_dedupe" in [action.name for action in diagnosis.proposed_actions]


def test_null_keys_read_as_a_null_spike(warehouse, settings) -> None:
    incident = _incident(failed_checks=[_check("not_null_units_sold", 0.4)])
    diagnosis = _diagnose(incident, warehouse, settings)
    assert diagnosis.root_cause is RootCause.NULL_SPIKE
    assert "quarantine_invalid_rows" in [a.name for a in diagnosis.proposed_actions]


def test_orphan_keys_read_as_a_referential_break(warehouse, settings) -> None:
    incident = _incident(failed_checks=[_check("referential_integrity_sku", 0.2)])
    diagnosis = _diagnose(incident, warehouse, settings)
    assert diagnosis.root_cause is RootCause.REFERENTIAL_BREAK


def test_row_count_collapse_reads_as_a_partial_delivery(warehouse, settings) -> None:
    incident = _incident(anomalies=[_signal("row_count", 180.0, 520.0)])
    diagnosis = _diagnose(incident, warehouse, settings)
    assert diagnosis.root_cause is RootCause.VOLUME_DROP
    assert "hold_downstream" in [action.name for action in diagnosis.proposed_actions]


def test_value_jump_on_stable_volume_reads_as_a_unit_change(warehouse, settings) -> None:
    incident = _incident(
        anomalies=[
            _signal("row_count", 512.0, 520.0),
            _signal("measure_avg__units_sold", 54.0, 4.5),
            _signal("measure_sum__units_sold", 27000.0, 2300.0),
        ]
    )
    diagnosis = _diagnose(incident, warehouse, settings)
    assert diagnosis.root_cause is RootCause.UNIT_OF_MEASURE

    correction = next(
        action for action in diagnosis.proposed_actions if action.name == "apply_unit_correction"
    )
    assert correction.arguments["column_name"] == "units_sold"
    assert correction.arguments["factor"] == pytest.approx(4.5 / 54.0, rel=1e-3)


def test_value_jump_with_extra_rows_is_not_a_unit_change(warehouse, settings) -> None:
    """More rows and more value together is demand, not a unit change."""
    incident = _incident(
        anomalies=[
            _signal("row_count", 900.0, 520.0),
            _signal("measure_avg__units_sold", 12.0, 4.5),
        ]
    )
    diagnosis = _diagnose(incident, warehouse, settings)
    assert diagnosis.root_cause is not RootCause.UNIT_OF_MEASURE


def test_unexplained_anomaly_escalates_instead_of_guessing(warehouse, settings) -> None:
    incident = _incident(anomalies=[_signal("measure_sum__net_sales_usd", 9000.0, 8000.0)])
    diagnosis = _diagnose(incident, warehouse, settings)
    assert diagnosis.root_cause is RootCause.UNKNOWN
    assert diagnosis.confidence < 0.5
    assert [action.name for action in diagnosis.proposed_actions] == ["open_upstream_ticket"]


def test_schema_drift_finds_the_renamed_column(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("inventory", FaultType.SCHEMA_DRIFT))
    refresh_run(business_date, warehouse, settings, "test", detect=False)

    incident = _incident(
        dataset="inventory",
        business_date=business_date,
        failed_checks=[_check("schema_complete")],
    )
    diagnosis = _diagnose(incident, warehouse, settings)

    assert diagnosis.root_cause is RootCause.SCHEMA_DRIFT
    mapping = next(
        action for action in diagnosis.proposed_actions if action.name == "add_column_mapping"
    )
    assert mapping.arguments["canonical_column"] == "on_hand_cases"
    assert mapping.arguments["source_column"] == "on_hand_cases_qty"


# -- investigation tools ---------------------------------------------------


def test_tools_expose_the_extract_schema(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("inventory", FaultType.SCHEMA_DRIFT))
    refresh_run(business_date, warehouse, settings, "test", detect=False)

    toolbox = Toolbox(warehouse, business_date)
    columns = toolbox.get_partition_columns("inventory")
    assert "on_hand_cases_qty" in columns["columns_in_extract"]
    assert columns["unresolved_columns"] == ["on_hand_cases"]


def test_tools_compare_group_counts_across_days(settings, warehouse, inject) -> None:
    first, second = date(2025, 3, 1), date(2025, 3, 2)
    inject(first)
    inject(second, FaultSpec("pos_sales", FaultType.VOLUME_DROP, magnitude=0.7))
    refresh_run(first, warehouse, settings, "test", detect=False)
    refresh_run(second, warehouse, settings, "test", detect=False)

    toolbox = Toolbox(warehouse, second)
    result = toolbox.count_rows_by("pos_sales", "store_id")
    assert result["groups_today"] < result["groups_previous"]


def test_unknown_tool_returns_an_error_not_an_exception(warehouse) -> None:
    toolbox = Toolbox(warehouse, date(2025, 3, 1))
    assert "error" in toolbox.call("drop_everything", {})


def test_tool_failure_is_reported_to_the_model(warehouse) -> None:
    toolbox = Toolbox(warehouse, date(2025, 3, 1))
    result = toolbox.call("get_metric_history", {"dataset": "nope", "metric": "row_count"})
    assert "error" in result or result["points"] == []


# -- engine selection and fallback ----------------------------------------


def test_engine_selection_falls_back_without_a_key(settings, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings.agent.provider = AgentProvider.AUTO
    assert build_engine(settings).name == "rules"


def test_forcing_claude_without_a_key_fails_loudly(settings, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings.agent.provider = AgentProvider.CLAUDE
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_engine(settings)


class _StubMessages:
    """Minimal stand-in for ``client.messages``."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _StubClient:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _StubMessages(responses)


class _Block:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Response:
    def __init__(self, content: list[Any], stop_reason: str = "tool_use") -> None:
        self.content = content
        self.stop_reason = stop_reason


def _submit_block(**payload: Any) -> _Block:
    return _Block(type="tool_use", id="toolu_1", name="submit_diagnosis", input=payload)


def test_claude_engine_parses_a_submitted_diagnosis(settings, warehouse) -> None:
    client = _StubClient(
        [
            _Response(
                [
                    _Block(
                        type="tool_use",
                        id="toolu_0",
                        name="get_partition_columns",
                        input={"dataset": "pos_sales"},
                    )
                ]
            ),
            _Response(
                [
                    _submit_block(
                        root_cause="duplicate_batch",
                        confidence=0.82,
                        summary="The batch was delivered twice.",
                        reasoning="Row count doubled while distinct keys did not.",
                        proposed_actions=[
                            {"name": "enable_dedupe", "arguments": {"dataset": "pos_sales"}},
                            {"name": "rerun_pipeline"},
                        ],
                    )
                ]
            ),
        ]
    )
    engine = ClaudeEngine(settings.agent, client=client)  # type: ignore[arg-type]
    diagnosis = engine.diagnose(
        DiagnosisRequest(incident=_incident(), warehouse=warehouse, settings=settings)
    )

    assert diagnosis.root_cause is RootCause.DUPLICATE_BATCH
    assert diagnosis.confidence == pytest.approx(0.82)
    assert [action.name for action in diagnosis.proposed_actions] == [
        "enable_dedupe",
        "rerun_pipeline",
    ]
    assert diagnosis.engine.startswith("claude:")
    assert diagnosis.tool_calls == 1
    assert "temperature" not in client.messages.calls[0]


def test_claude_engine_parses_contributing_causes(settings, warehouse) -> None:
    client = _StubClient(
        [
            _Response(
                [
                    _submit_block(
                        root_cause="duplicate_batch",
                        contributing_causes=[
                            "unit_of_measure",
                            "duplicate_batch",  # the primary again: dropped
                            "not_a_cause",  # outside the vocabulary: dropped
                            "unit_of_measure",  # repeated: dropped
                        ],
                        confidence=0.7,
                        summary="Delivered twice and in cases.",
                        reasoning="Duplicate keys, and units per row up 12x.",
                        proposed_actions=[{"name": "enable_dedupe"}],
                    )
                ]
            ),
        ]
    )
    engine = ClaudeEngine(settings.agent, client=client)  # type: ignore[arg-type]
    diagnosis = engine.diagnose(
        DiagnosisRequest(incident=_incident(), warehouse=warehouse, settings=settings)
    )
    assert diagnosis.root_cause is RootCause.DUPLICATE_BATCH
    assert diagnosis.contributing_causes == [RootCause.UNIT_OF_MEASURE]


def test_claude_engine_degrades_to_rules_on_api_error(settings, warehouse) -> None:
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.example"))
    engine = ClaudeEngine(settings.agent, client=_StubClient([error]))  # type: ignore[arg-type]
    incident = _incident(failed_checks=[_check("partition_present")])

    diagnosis = engine.diagnose(
        DiagnosisRequest(incident=incident, warehouse=warehouse, settings=settings)
    )

    assert diagnosis.root_cause is RootCause.LATE_ARRIVAL
    assert "fallback" in diagnosis.engine
    assert any(event.event == "agent.degraded" for event in incident.timeline)


def test_claude_engine_degrades_when_no_diagnosis_is_submitted(settings, warehouse) -> None:
    engine = ClaudeEngine(  # type: ignore[arg-type]
        settings.agent,
        client=_StubClient([_Response([_Block(type="text", text="I am thinking")], "end_turn")]),
    )
    diagnosis = engine.diagnose(
        DiagnosisRequest(
            incident=_incident(failed_checks=[_check("unique_natural_key", 0.5)]),
            warehouse=warehouse,
            settings=settings,
        )
    )
    assert diagnosis.root_cause is RootCause.DUPLICATE_BATCH
    assert "fallback" in diagnosis.engine


def test_monitor_opens_and_diagnoses_an_incident(settings, seeded, next_day, inject) -> None:
    inject(next_day, FaultSpec("shipments", FaultType.DUPLICATE_BATCH, magnitude=0.5))
    result = monitor(next_day, seeded, settings, "test")

    assert len(result.incidents) == 1
    incident = result.incidents[0]
    assert incident.dataset == "shipments"
    assert incident.diagnosis is not None
    assert incident.diagnosis.root_cause is RootCause.DUPLICATE_BATCH


def test_monitor_stays_quiet_on_a_clean_day(settings, seeded, next_day, inject) -> None:
    inject(next_day)
    result = monitor(next_day, seeded, settings, "test")
    assert result.incidents == []
