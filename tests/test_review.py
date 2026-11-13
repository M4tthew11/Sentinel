"""The action gate: hard rules, the reviewer, and how the executor combines them."""

from __future__ import annotations

from datetime import date
from typing import Any

import anthropic
import httpx
import pytest

from sentinel.config import RemediationMode, ReviewerProvider
from sentinel.evals.review import (
    ProbeResult,
    ReviewEvalReport,
    build_review_scenario,
    run_review_evals,
)
from sentinel.incidents.models import (
    ActionReview,
    ActionStatus,
    Diagnosis,
    Incident,
    IncidentStatus,
    ProposedAction,
    ReviewVerdict,
    RootCause,
)
from sentinel.remediation.actions import ACTIONS
from sentinel.remediation.executor import RemediationExecutor
from sentinel.remediation.policy import hard_rules
from sentinel.review.base import ActionUnderReview, ReviewRequest, build_reviewer
from sentinel.review.claude_reviewer import ClaudeReviewer
from sentinel.review.prompts import review_brief
from sentinel.review.rules import RuleReviewer

DAY = date(2025, 3, 1)


def _incident(
    actions: list[ProposedAction] | None = None,
    dataset: str = "shipments",
    checks: tuple[str, ...] = (),
    **overrides: Any,
) -> Incident:
    fields: dict[str, Any] = {
        "run_id": "RUN-test",
        "business_date": DAY,
        "dataset": dataset,
        "severity": "critical",
        "title": "test",
        "failed_checks": [{"check": name, "severity": "critical"} for name in checks],
        "diagnosis": Diagnosis(
            root_cause=RootCause.DUPLICATE_BATCH,
            confidence=0.99,
            summary="SECRET-SUMMARY",
            reasoning="SECRET-REASONING",
            proposed_actions=actions or [],
            engine="test",
        ),
    }
    fields.update(overrides)
    return Incident(**fields)


class _FixedReviewer:
    """Gives every action the same verdict and remembers what it was shown."""

    name = "fixed"

    def __init__(self, verdict: ReviewVerdict, reviews: int | None = None) -> None:
        self.verdict = verdict
        self.reviews = reviews
        self.requests: list[ReviewRequest] = []

    def review(self, request: ReviewRequest) -> list[ActionReview]:
        self.requests.append(request)
        count = len(request.actions) if self.reviews is None else self.reviews
        return [
            ActionReview(verdict=self.verdict, reason="because", reviewer=self.name)
            for _ in range(count)
        ]


class _BrokenReviewer:
    name = "broken"

    def review(self, request: ReviewRequest) -> list[ActionReview]:
        raise RuntimeError("reviewer exploded")


def _apply(settings, warehouse, incident: Incident, reviewer: Any):
    return RemediationExecutor(warehouse, settings, reviewer=reviewer).apply(incident)


# -- hard rules -------------------------------------------------------------


def test_high_risk_is_a_hard_rule() -> None:
    rules = hard_rules(ACTIONS["apply_unit_correction"], {}, _incident())
    assert rules == ["risk=high"]


def test_low_risk_in_scope_trips_no_rule() -> None:
    arguments = {"dataset": "shipments", "business_date": DAY.isoformat()}
    assert hard_rules(ACTIONS["quarantine_partition"], arguments, _incident()) == []


def test_another_dataset_is_out_of_scope() -> None:
    rules = hard_rules(ACTIONS["enable_dedupe"], {"dataset": "pos_sales"}, _incident())
    assert len(rules) == 1 and "dataset=pos_sales" in rules[0]


def test_another_business_date_is_out_of_scope() -> None:
    rules = hard_rules(
        ACTIONS["quarantine_partition"], {"business_date": "2025-02-28"}, _incident()
    )
    assert len(rules) == 1 and "2025-02-28" in rules[0]


def test_an_unreadable_date_is_out_of_scope() -> None:
    rules = hard_rules(ACTIONS["quarantine_partition"], {"business_date": "tomorrow"}, _incident())
    assert len(rules) == 1 and "not a date" in rules[0]


# -- the executor's gate ----------------------------------------------------


def test_an_approved_low_risk_action_runs(settings, warehouse) -> None:
    incident = _incident([ProposedAction(name="enable_dedupe")])
    outcome = _apply(settings, warehouse, incident, _FixedReviewer(ReviewVerdict.APPROVE))

    assert [record.name for record in outcome.executed] == ["enable_dedupe"]
    assert incident.actions[0].review is not None
    assert incident.actions[0].review.verdict is ReviewVerdict.APPROVE


def test_an_escalated_action_waits_for_a_human(settings, warehouse) -> None:
    incident = _incident([ProposedAction(name="enable_dedupe")])
    outcome = _apply(settings, warehouse, incident, _FixedReviewer(ReviewVerdict.ESCALATE))

    assert outcome.executed == []
    record = incident.actions[0]
    assert record.status is ActionStatus.AWAITING_APPROVAL
    assert record.result == "reviewer escalated: because"
    assert incident.status is IncidentStatus.AWAITING_APPROVAL


def test_a_human_can_still_approve_what_the_reviewer_escalated(settings, warehouse) -> None:
    incident = _incident([ProposedAction(name="hold_downstream", arguments={"reason": "x"})])
    executor = RemediationExecutor(
        warehouse, settings, reviewer=_FixedReviewer(ReviewVerdict.ESCALATE)
    )
    executor.apply(incident)
    outcome = executor.approve(incident, 0, "alice")

    assert outcome.executed[0].actor == "alice"
    assert incident.actions[0].status is ActionStatus.EXECUTED


def test_reviewer_approval_cannot_unlock_a_high_risk_action(settings, warehouse) -> None:
    incident = _incident(
        [
            ProposedAction(
                name="apply_unit_correction",
                arguments={"column_name": "cases_shipped", "factor": 0.5},
            )
        ]
    )
    outcome = _apply(settings, warehouse, incident, _FixedReviewer(ReviewVerdict.APPROVE))

    assert outcome.executed == []
    record = incident.actions[0]
    assert record.status is ActionStatus.AWAITING_APPROVAL
    assert record.hard_rules == ["risk=high"]
    assert record.result == "risk=high requires human approval"
    # The opinion is kept for the human who decides.
    assert record.review is not None and record.review.verdict is ReviewVerdict.APPROVE


def test_reviewer_approval_cannot_unlock_an_out_of_scope_action(settings, warehouse) -> None:
    incident = _incident(
        [ProposedAction(name="quarantine_partition", arguments={"business_date": "2025-02-28"})]
    )
    outcome = _apply(settings, warehouse, incident, _FixedReviewer(ReviewVerdict.APPROVE))

    assert outcome.executed == []
    assert incident.actions[0].status is ActionStatus.AWAITING_APPROVAL
    assert "outside the incident's scope" in incident.actions[0].result


def test_reviewer_approval_cannot_override_manual_mode(settings, warehouse) -> None:
    settings.remediation.mode = RemediationMode.MANUAL
    incident = _incident([ProposedAction(name="enable_dedupe")])
    outcome = _apply(settings, warehouse, incident, _FixedReviewer(ReviewVerdict.APPROVE))

    assert outcome.executed == []
    assert incident.actions[0].status is ActionStatus.AWAITING_APPROVAL


def test_a_failing_reviewer_escalates_everything(settings, warehouse) -> None:
    incident = _incident(
        [ProposedAction(name="enable_dedupe"), ProposedAction(name="rerun_pipeline")]
    )
    outcome = _apply(settings, warehouse, incident, _BrokenReviewer())

    assert outcome.executed == []
    assert all(record.status is ActionStatus.AWAITING_APPROVAL for record in incident.actions)
    assert any(event.event == "review.failed" for event in incident.timeline)


def test_an_action_the_reviewer_skipped_is_escalated(settings, warehouse) -> None:
    incident = _incident(
        [ProposedAction(name="enable_dedupe"), ProposedAction(name="rerun_pipeline")]
    )
    outcome = _apply(
        settings, warehouse, incident, _FixedReviewer(ReviewVerdict.APPROVE, reviews=1)
    )

    assert [record.name for record in outcome.executed] == ["enable_dedupe"]
    assert incident.actions[1].status is ActionStatus.AWAITING_APPROVAL


def test_the_reviewer_never_sees_the_proposers_words(settings, warehouse) -> None:
    reviewer = _FixedReviewer(ReviewVerdict.ESCALATE)
    incident = _incident(
        [
            ProposedAction(
                name="open_upstream_ticket",
                arguments={"summary": "SECRET-ARGUMENT", "body": "SECRET-ARGUMENT"},
                rationale="SECRET-RATIONALE",
            ),
            ProposedAction(name="DROP TABLE x"),
        ]
    )
    _apply(settings, warehouse, incident, reviewer)

    request = reviewer.requests[0]
    # Only whitelisted, valid actions are worth a review.
    assert [action.name for action in request.actions] == ["open_upstream_ticket"]
    assert request.actions[0].arguments == {}
    assert "SECRET" not in review_brief(request.incident, request.actions)


def test_every_verdict_is_on_the_timeline(settings, warehouse) -> None:
    incident = _incident([ProposedAction(name="enable_dedupe")])
    _apply(settings, warehouse, incident, _FixedReviewer(ReviewVerdict.ESCALATE))

    reviewed = [event for event in incident.timeline if event.event == "action.reviewed"]
    assert len(reviewed) == 1
    assert reviewed[0].actor == "fixed"
    assert reviewed[0].detail == "enable_dedupe: escalate - because"
    assert Incident.from_json(incident.to_json()).actions[0].review == incident.actions[0].review


def test_with_the_reviewer_off_the_risk_level_alone_decides(settings, warehouse) -> None:
    assert settings.reviewer.provider is ReviewerProvider.OFF
    incident = _incident([ProposedAction(name="enable_dedupe")])
    outcome = RemediationExecutor(warehouse, settings).apply(incident)

    assert [record.name for record in outcome.executed] == ["enable_dedupe"]
    assert incident.actions[0].review is None


def test_plan_decides_without_executing(settings, warehouse) -> None:
    incident = _incident()
    executor = RemediationExecutor(
        warehouse, settings, reviewer=_FixedReviewer(ReviewVerdict.APPROVE)
    )
    records = executor.plan(incident, [ProposedAction(name="enable_dedupe")])

    assert records[0].status is ActionStatus.PROPOSED
    assert warehouse.fetch_one("SELECT count(*) FROM ops_flag")[0] == 0


# -- the rule reviewer ------------------------------------------------------


def _rule_verdict(incident: Incident, name: str, **arguments: Any) -> ActionReview:
    request = ReviewRequest(
        incident=incident,
        actions=[ActionUnderReview(name=name, arguments=arguments)],
        warehouse=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    return RuleReviewer().review(request)[0]


def test_rules_approve_dedupe_only_when_keys_are_duplicated() -> None:
    duplicated = _incident(checks=("unique_natural_key",))
    assert _rule_verdict(duplicated, "enable_dedupe").verdict is ReviewVerdict.APPROVE
    assert _rule_verdict(_incident(), "enable_dedupe").verdict is ReviewVerdict.ESCALATE


def test_rules_approve_containment() -> None:
    for name in ("hold_downstream", "open_upstream_ticket", "rerun_pipeline"):
        assert _rule_verdict(_incident(), name).verdict is ReviewVerdict.APPROVE


def test_rules_never_vouch_for_dropping_a_partition() -> None:
    incident = _incident(checks=("unique_natural_key", "not_null_cases_shipped"))
    assert _rule_verdict(incident, "quarantine_partition").verdict is ReviewVerdict.ESCALATE


def test_rules_never_vouch_for_a_unit_correction() -> None:
    incident = _incident(
        dataset="pos_sales",
        anomalies=[{"metric": "measure_sum__units_sold", "ratio": 12.0}],
    )
    matching = _rule_verdict(
        incident, "apply_unit_correction", column_name="units_sold", factor=1 / 12
    )
    inverted = _rule_verdict(incident, "apply_unit_correction", column_name="units_sold", factor=12)

    assert matching.verdict is ReviewVerdict.ESCALATE and "matches" in matching.reason
    assert inverted.verdict is ReviewVerdict.ESCALATE and "does not undo" in inverted.reason


def test_rules_check_a_column_mapping_against_the_extract() -> None:
    incident = _incident(
        dataset="inventory",
        checks=("schema_complete",),
        evidence={
            "columns_in_extract": ["snapshot_date", "dc_id", "sku_id", "on_hand_cases_qty"],
            "expected_columns": ["snapshot_date", "dc_id", "sku_id", "on_hand_cases"],
        },
    )

    def verdict(canonical: str, source: str) -> ReviewVerdict:
        return _rule_verdict(
            incident, "add_column_mapping", canonical_column=canonical, source_column=source
        ).verdict

    assert verdict("on_hand_cases", "on_hand_cases_qty") is ReviewVerdict.APPROVE
    assert verdict("on_hand_cases", "dc_id") is ReviewVerdict.ESCALATE
    assert verdict("on_hand_cases", "not_there") is ReviewVerdict.ESCALATE
    assert verdict("sku_id", "on_hand_cases_qty") is ReviewVerdict.ESCALATE
    assert verdict("made_up", "on_hand_cases_qty") is ReviewVerdict.ESCALATE


def test_rules_quarantine_rows_only_when_most_of_the_partition_survives() -> None:
    def incident(rate: float, *extra: str) -> Incident:
        return _incident(
            dataset="inventory",
            checks=("not_null_on_hand_cases", *extra),
            evidence={"metrics": {"null_rate__on_hand_cases": rate}},
        )

    assert _rule_verdict(incident(0.35), "quarantine_invalid_rows").verdict is ReviewVerdict.APPROVE
    assert _rule_verdict(incident(0.9), "quarantine_invalid_rows").verdict is ReviewVerdict.ESCALATE
    # Nulls caused by a renamed column are a mapping problem, not bad rows.
    renamed = incident(0.35, "schema_complete")
    assert _rule_verdict(renamed, "quarantine_invalid_rows").verdict is ReviewVerdict.ESCALATE
    assert _rule_verdict(_incident(), "quarantine_invalid_rows").verdict is ReviewVerdict.ESCALATE


def test_rules_do_not_speak_for_another_dataset() -> None:
    incident = _incident(checks=("unique_natural_key",))
    verdict = _rule_verdict(incident, "enable_dedupe", dataset="pos_sales")
    assert verdict.verdict is ReviewVerdict.ESCALATE


# -- reviewer selection -----------------------------------------------------


def test_reviewer_selection_falls_back_without_a_key(settings, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings.reviewer.provider = ReviewerProvider.AUTO
    reviewer = build_reviewer(settings)
    assert reviewer is not None and reviewer.name == "rules"


def test_forcing_the_claude_reviewer_without_a_key_fails_loudly(settings, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings.reviewer.provider = ReviewerProvider.CLAUDE
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_reviewer(settings)


def test_reviewer_can_be_switched_off(settings) -> None:
    settings.reviewer.provider = ReviewerProvider.OFF
    assert build_reviewer(settings) is None


# -- the Claude reviewer ----------------------------------------------------


class _StubMessages:
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


def _submit(*reviews: dict[str, Any]) -> _Response:
    return _Response(
        [
            _Block(
                type="tool_use",
                id="toolu_9",
                name="submit_review",
                input={"reviews": list(reviews)},
            )
        ]
    )


def _claude_review(settings, warehouse, responses: list[Any], actions: int = 2):
    client = _StubClient(responses)
    reviewer = ClaudeReviewer(settings.reviewer, client=client)  # type: ignore[arg-type]
    request = ReviewRequest(
        incident=_incident(checks=("unique_natural_key",)),
        actions=[
            ActionUnderReview(name="enable_dedupe", arguments={}),
            ActionUnderReview(name="quarantine_partition", arguments={}),
        ][:actions],
        warehouse=warehouse,
        settings=settings,
    )
    return reviewer.review(request), client


def test_claude_reviewer_investigates_then_submits(settings, warehouse) -> None:
    lookup = _Response(
        [
            _Block(
                type="tool_use",
                id="toolu_0",
                name="get_failed_checks",
                input={"dataset": "shipments"},
            )
        ]
    )
    reviews, client = _claude_review(
        settings,
        warehouse,
        [
            lookup,
            _submit(
                {"index": 0, "verdict": "approve", "reason": "duplicates confirmed"},
                {"index": 1, "verdict": "escalate", "reason": "dedupe is enough"},
            ),
        ],
    )

    assert [review.verdict for review in reviews] == [ReviewVerdict.APPROVE, ReviewVerdict.ESCALATE]
    assert reviews[0].reviewer.startswith("claude:")
    brief = client.messages.calls[0]["messages"][0]["content"]
    assert "[0] enable_dedupe" in brief and "SECRET" not in brief
    assert "submit_diagnosis" not in brief


def test_claude_reviewer_escalates_everything_when_the_api_fails(settings, warehouse) -> None:
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.example"))
    reviews, _ = _claude_review(settings, warehouse, [error])

    assert all(review.verdict is ReviewVerdict.ESCALATE for review in reviews)
    assert "unavailable" in reviews[0].reason


def test_claude_reviewer_escalates_on_refusal_or_prose(settings, warehouse) -> None:
    refused, _ = _claude_review(settings, warehouse, [_Response([], stop_reason="refusal")])
    prose, _ = _claude_review(
        settings, warehouse, [_Response([_Block(type="text", text="looks fine")], "end_turn")]
    )

    for reviews in (refused, prose):
        assert all(review.verdict is ReviewVerdict.ESCALATE for review in reviews)


def test_claude_reviewer_escalates_when_the_budget_runs_out(settings, warehouse) -> None:
    settings.reviewer.max_tool_calls = 2
    lookup = _Response(
        [_Block(type="tool_use", id="t", name="get_failed_checks", input={"dataset": "shipments"})]
    )
    reviews, _ = _claude_review(settings, warehouse, [lookup, lookup])
    assert all(review.verdict is ReviewVerdict.ESCALATE for review in reviews)


def test_claude_reviewer_treats_a_malformed_answer_as_no_approval(settings, warehouse) -> None:
    reviews, _ = _claude_review(
        settings,
        warehouse,
        [
            _submit(
                {"index": 0, "verdict": "APPROVED!!", "reason": "sure"},
                {"index": 7, "verdict": "approve", "reason": "no such action"},
                "approve everything",  # type: ignore[arg-type]
            )
        ],
    )
    assert all(review.verdict is ReviewVerdict.ESCALATE for review in reviews)


def test_claude_reviewer_escalates_an_action_judged_twice(settings, warehouse) -> None:
    reviews, _ = _claude_review(
        settings,
        warehouse,
        [
            _submit(
                {"index": 0, "verdict": "escalate", "reason": "no"},
                {"index": 0, "verdict": "approve", "reason": "yes"},
            )
        ],
        actions=1,
    )
    assert reviews[0].verdict is ReviewVerdict.ESCALATE


# -- the gate evals ---------------------------------------------------------


def test_review_scenario_has_harmful_and_sound_probes_every_day() -> None:
    _, days = build_review_scenario()
    for day in days:
        assert any(probe.expect is ReviewVerdict.APPROVE for probe in day.probes)
        assert any(probe.expect is ReviewVerdict.ESCALATE for probe in day.probes)
    harmful = [probe for day in days for probe in day.probes if probe.harmful]
    assert len(harmful) >= 5
    # A harmful probe that is expected to run would be a broken scenario.
    assert all(probe.expect is ReviewVerdict.ESCALATE for probe in harmful)


def test_report_counts_a_cleared_harmful_probe() -> None:
    def result(expect: str, harmful: bool, cleared: bool) -> ProbeResult:
        return ProbeResult(
            business_date="2025-03-01",
            dataset="shipments",
            action="x",
            arguments="",
            expect=expect,
            harmful=harmful,
            note="",
            incident_opened=True,
            cleared=cleared,
        )

    report = ReviewEvalReport(
        reviewer="test",
        results=[
            result("escalate", True, True),
            result("escalate", False, False),
            result("approve", False, True),
            result("approve", False, False),
        ],
    )
    assert report.harmful_approved == 1
    assert report.wrongly_approved == 1
    assert report.pre_approval_rate == pytest.approx(0.5)
    assert report.accuracy == pytest.approx(0.5)


def test_no_harmful_action_gets_past_the_rule_reviewer(settings) -> None:
    settings.reviewer.provider = ReviewerProvider.RULES
    report = run_review_evals(settings)

    assert all(result.incident_opened for result in report.results)
    assert report.harmful_approved == 0
    assert report.wrongly_approved == 0
    # Safe is not enough: a gate that sends everything to a human is no gate.
    assert report.pre_approval_rate == 1.0


def test_the_standard_scenario_heals_the_same_with_the_rule_reviewer(settings) -> None:
    from sentinel.evals.harness import run_evals

    settings.reviewer.provider = ReviewerProvider.OFF
    without = [case.final_status for case in run_evals(settings).cases]
    settings.reviewer.provider = ReviewerProvider.RULES
    reviewed = [case.final_status for case in run_evals(settings).cases]

    assert reviewed == without
