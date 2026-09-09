"""Scoring logic of the eval harness.

The harness itself replays a 50-day scenario, which is a demo rather than a unit
test; what is worth pinning down here is that it scores honestly - no partial
credit for naming the wrong fault, and no free pass for an engine that opens an
incident on a healthy day.
"""

from __future__ import annotations

from datetime import date

from sentinel.evals.harness import EvalCase, EvalReport, _score
from sentinel.incidents.models import Diagnosis, Incident, ProposedAction, RootCause
from sentinel.scenario import DEFAULT_SCENARIO, ScenarioDay, build_scenario


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
