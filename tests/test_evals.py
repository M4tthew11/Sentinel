"""Scoring logic of the eval harness.

The harness itself replays a 50-day scenario, which is a demo rather than a unit
test; what is worth pinning down here is that it scores honestly - no partial
credit for naming the wrong fault, and no free pass for an engine that opens an
incident on a healthy day.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from sentinel.evals.harness import (
    EvalCase,
    EvalReport,
    RepeatedEvalReport,
    _score,
    run_repeated_evals,
)
from sentinel.evals.reports import save_report
from sentinel.generator.faults import OFF_CATALOGUE
from sentinel.generator.source import fault_specs
from sentinel.incidents.models import Diagnosis, Incident, ProposedAction, RootCause
from sentinel.scenario import (
    DEFAULT_SCENARIO,
    SCENARIOS,
    ScenarioDay,
    build_hard_scenario,
    build_recurring_scenario,
    build_scenario,
)


def _case(**overrides) -> EvalCase:
    payload = {
        "business_date": "2025-03-01",
        "dataset": "pos_sales",
        "expected_root_cause": "volume_drop",
        "observed_root_cause": "volume_drop",
        "expected_actions": ("hold_downstream",),
        "proposed_actions": ("hold_downstream", "open_upstream_ticket"),
        "incident_opened": True,
    }
    payload.update(overrides)
    return EvalCase(**payload)  # type: ignore[arg-type]


def test_correct_diagnosis_scores_as_correct() -> None:
    case = _case()
    assert case.detected and case.root_cause_correct and case.actions_correct


def test_wrong_root_cause_gets_no_partial_credit() -> None:
    case = _case(observed_root_cause="null_spike")
    assert not case.root_cause_correct
    assert case.detected, "detection and diagnosis are scored separately"


def test_missing_expected_action_fails_the_action_score() -> None:
    case = _case(proposed_actions=("open_upstream_ticket",))
    assert not case.actions_correct


def test_extra_actions_are_tolerated() -> None:
    case = _case(proposed_actions=("hold_downstream", "rerun_pipeline", "open_upstream_ticket"))
    assert case.actions_correct


def test_a_missed_incident_fails_detection() -> None:
    case = _case(incident_opened=False, observed_root_cause="none")
    assert not case.detected
    assert not case.root_cause_correct


def test_a_false_positive_fails_detection() -> None:
    case = _case(expected_root_cause="none", observed_root_cause="unknown")
    assert not case.detected
    assert not case.actions_correct


def test_report_aggregates_only_incident_cases() -> None:
    report = EvalReport(
        engine="rules",
        cases=[
            _case(),
            _case(observed_root_cause="null_spike"),
            _case(
                expected_root_cause="none",
                observed_root_cause="none",
                incident_opened=False,
                proposed_actions=(),
            ),
        ],
    )
    assert len(report.incident_cases) == 2
    assert report.root_cause_accuracy == 0.5
    assert report.detection_rate == 1.0


def test_scoring_reads_the_incident_for_the_right_dataset() -> None:
    day = ScenarioDay(
        business_date=date(2025, 3, 1),
        dataset="inventory",
        expected_root_cause=RootCause.NULL_SPIKE,
        expected_actions=("quarantine_invalid_rows",),
    )
    unrelated = Incident(
        run_id="RUN-1", business_date=day.business_date, dataset="pos_sales", title="noise"
    )
    relevant = Incident(
        run_id="RUN-1",
        business_date=day.business_date,
        dataset="inventory",
        title="nulls",
        diagnosis=Diagnosis(
            root_cause=RootCause.NULL_SPIKE,
            confidence=0.9,
            proposed_actions=[ProposedAction(name="quarantine_invalid_rows")],
            engine="rules",
        ),
    )

    case = _score(day, [unrelated, relevant])
    assert case.root_cause_correct
    assert case.actions_correct


def test_scenario_covers_every_fault_type_once() -> None:
    causes = [day.expected_root_cause for day in DEFAULT_SCENARIO.incident_days]
    assert len(causes) == len(set(causes)) == 7
    assert RootCause.UNKNOWN not in causes


def test_scenario_ends_with_a_control_day() -> None:
    """The persistent rename means the last day is only clean if the fix worked."""
    scenario = build_scenario(date(2025, 1, 6))
    control = scenario.days[-1]
    assert not control.is_incident
    assert control.faults, "the injected rename must still be in force"
    assert len(scenario.history_dates) >= 40


# -- compound and off-catalogue scoring -------------------------------------


def _compound(**overrides) -> EvalCase:
    payload = {
        "expected_root_cause": "duplicate_batch",
        "expected_contributing": ("unit_of_measure",),
        "observed_root_cause": "duplicate_batch",
        "expected_actions": ("enable_dedupe", "apply_unit_correction"),
        "proposed_actions": ("enable_dedupe", "rerun_pipeline"),
    }
    payload.update(overrides)
    return _case(**payload)


def test_either_injected_cause_is_an_acceptable_headline() -> None:
    assert _compound().root_cause_correct
    assert _compound(observed_root_cause="unit_of_measure").root_cause_correct
    assert not _compound(observed_root_cause="null_spike").root_cause_correct


def test_naming_only_one_of_two_causes_is_half_coverage() -> None:
    assert _compound().cause_coverage == 0.5
    full = _compound(observed_contributing=("unit_of_measure",))
    assert full.cause_coverage == 1.0


def test_fixing_half_a_compound_fault_fails_the_action_score() -> None:
    assert not _compound().actions_correct
    fixed = _compound(proposed_actions=("enable_dedupe", "apply_unit_correction"))
    assert fixed.actions_correct


def test_a_forbidden_action_is_harmful_even_alongside_the_right_ones() -> None:
    case = _case(
        expected_root_cause="unit_of_measure",
        observed_root_cause="unit_of_measure",
        expected_actions=("hold_downstream", "open_upstream_ticket"),
        proposed_actions=("hold_downstream", "apply_unit_correction", "open_upstream_ticket"),
        forbidden_actions=("apply_unit_correction",),
    )
    assert case.root_cause_correct
    assert case.harmful
    assert not case.actions_correct


def test_report_aggregates_coverage_and_harm() -> None:
    report = EvalReport(
        engine="rules",
        cases=[
            _compound(),
            _compound(
                observed_contributing=("unit_of_measure",),
                proposed_actions=("apply_unit_correction",),
                forbidden_actions=("apply_unit_correction",),
            ),
        ],
    )
    assert report.cause_coverage == 0.75
    assert report.harmful_rate == 0.5


def test_scoring_carries_contributing_causes_and_status() -> None:
    day = ScenarioDay(
        business_date=date(2025, 3, 1),
        dataset="pos_sales",
        expected_root_cause=RootCause.DUPLICATE_BATCH,
        contributing_causes=(RootCause.UNIT_OF_MEASURE,),
        expected_actions=("enable_dedupe",),
        forbidden_actions=("quarantine_partition",),
    )
    incident = Incident(
        run_id="RUN-1",
        business_date=day.business_date,
        dataset="pos_sales",
        title="dupes",
        diagnosis=Diagnosis(
            root_cause=RootCause.UNIT_OF_MEASURE,
            contributing_causes=[RootCause.DUPLICATE_BATCH],
            proposed_actions=[ProposedAction(name="enable_dedupe")],
        ),
    )
    case = _score(day, [incident])
    assert case.expected_causes == ("duplicate_batch", "unit_of_measure")
    assert case.cause_coverage == 1.0
    assert case.forbidden_actions == ("quarantine_partition",)
    assert case.final_status == str(incident.status)


def test_hard_scenario_mixes_compound_and_off_catalogue_days() -> None:
    scenario = build_hard_scenario()
    assert scenario.name == "hard"
    injected = [
        spec
        for day in scenario.days
        for value in day.faults.values()
        for spec in fault_specs(value)
    ]
    assert any(
        len(fault_specs(value)) > 1 for day in scenario.days for value in day.faults.values()
    )
    assert {spec.fault_type for spec in injected} & OFF_CATALOGUE == OFF_CATALOGUE
    assert any(day.forbidden_actions for day in scenario.days)
    assert not scenario.days[-1].is_incident, "a clean control day keeps false positives honest"
    assert {"standard", "hard"} <= set(SCENARIOS)


# -- repeated runs -----------------------------------------------------------


def _report(*cases: EvalCase) -> EvalReport:
    return EvalReport(engine="claude:test", scenario="hard", cases=list(cases))


def test_repeated_report_averages_and_spreads() -> None:
    good = _case()
    bad = _case(observed_root_cause="null_spike", proposed_actions=())
    repeated = RepeatedEvalReport(runs=[_report(good), _report(bad), _report(good)])

    assert repeated.mean("root_cause_accuracy") == pytest.approx(2 / 3)
    assert repeated.spread("root_cause_accuracy") == (0.0, 1.0)
    assert repeated.day_tally(0, "root_cause_correct") == 2
    assert repeated.day_tally(0, "actions_correct") == 2


def test_repeated_payload_carries_tallies_and_spread(tmp_path) -> None:
    harmful = _case(
        proposed_actions=("hold_downstream", "apply_unit_correction"),
        forbidden_actions=("apply_unit_correction",),
    )
    repeated = RepeatedEvalReport(runs=[_report(_case()), _report(harmful)])
    path = save_report(repeated, tmp_path)

    payload = json.loads(path.read_text())
    assert payload["runs"] == 2
    assert payload["summary"]["harmful_rate"] == 0.5
    assert payload["spread"]["harmful_rate"] == [0.0, 1.0]
    assert payload["cases"][0]["runs_harmful"] == 1
    assert payload["cases"][0]["runs_actions_correct"] == 1


def test_single_run_payload_says_one_run(tmp_path) -> None:
    payload = json.loads(save_report(_report(_case()), tmp_path).read_text())
    assert payload["runs"] == 1
    assert "spread" not in payload


def test_repeat_must_be_positive() -> None:
    with pytest.raises(ValueError):
        run_repeated_evals(repeat=0)


# -- incident history ---------------------------------------------------------


def test_score_records_whether_history_was_consulted() -> None:
    day = DEFAULT_SCENARIO.incident_days[0]

    def scored(tools_used: list[str]) -> EvalCase:
        incident = Incident(
            run_id="RUN-1",
            business_date=day.business_date,
            dataset=day.dataset or "pos_sales",
            diagnosis=Diagnosis(root_cause=RootCause.VOLUME_DROP, tools_used=tools_used),
        )
        return _score(day, [incident])

    assert scored(["find_similar_incidents", "count_rows_by"]).consulted_history
    assert not scored(["count_rows_by"]).consulted_history

    report = EvalReport(engine="stub", cases=[scored(["find_similar_incidents"]), scored([])])
    assert report.history_use_rate == pytest.approx(0.5)


def test_recurring_scenario_repeats_faults_and_sets_a_lookalike_trap() -> None:
    scenario = build_recurring_scenario()
    assert SCENARIOS["recurring"]().name == "recurring"

    def shape(day: ScenarioDay) -> tuple[str, tuple[str, ...]]:
        return (
            day.dataset or "-",
            tuple(
                sorted(
                    str(spec.fault_type)
                    for value in day.faults.values()
                    for spec in fault_specs(value)
                )
            ),
        )

    shapes = [shape(day) for day in scenario.incident_days]
    repeats = {item for item in shapes if shapes.count(item) == 2}
    assert len(repeats) == 2, "two faults must occur twice, so the second has a precedent"

    partial, full = (
        day
        for day in scenario.incident_days
        if day.expected_root_cause is RootCause.UNIT_OF_MEASURE
    )
    assert partial.business_date < full.business_date
    assert "apply_unit_correction" in partial.forbidden_actions
    assert full.expected_actions == ("apply_unit_correction",)
    assert not scenario.days[-1].is_incident
