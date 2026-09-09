"""The remediation boundary: whitelist, argument validation, and the risk gate."""

from __future__ import annotations

from datetime import date

import pytest

from sentinel.config import RemediationMode
from sentinel.elt import ops
from sentinel.generator.faults import FaultSpec, FaultType
from sentinel.incidents.models import (
    ActionStatus,
    Diagnosis,
    Incident,
    IncidentStatus,
    ProposedAction,
    RiskLevel,
    RootCause,
)
from sentinel.incidents.store import IncidentStore
from sentinel.remediation.actions import ACTIONS
from sentinel.remediation.executor import RemediationExecutor
from sentinel.runner import refresh_run


def _incident(actions: list[ProposedAction], business_date: date, dataset: str = "pos_sales"):
    return Incident(
        run_id="RUN-test",
        business_date=business_date,
        dataset=dataset,
        severity="critical",
        title="test",
        diagnosis=Diagnosis(
            root_cause=RootCause.DUPLICATE_BATCH,
            confidence=0.9,
            summary="test",
            proposed_actions=actions,
            engine="test",
        ),
    )


def test_unknown_actions_never_execute(settings, warehouse) -> None:
    incident = _incident([ProposedAction(name="DROP TABLE gold_sku_daily")], date(2025, 3, 1))
    outcome = RemediationExecutor(warehouse, settings).apply(incident)

    assert outcome.executed == []
    assert outcome.failed[0].status is ActionStatus.FAILED
    assert "whitelist" in outcome.failed[0].result


def test_invalid_arguments_are_rejected_before_execution(settings, warehouse) -> None:
    incident = _incident(
        [ProposedAction(name="add_column_mapping", arguments={"canonical_column": "units_sold"})],
        date(2025, 3, 1),
    )
    outcome = RemediationExecutor(warehouse, settings).apply(incident)
    assert outcome.executed == []
    assert "missing argument" in outcome.failed[0].result


def test_unknown_argument_names_are_rejected(settings, warehouse) -> None:
    incident = _incident(
        [ProposedAction(name="enable_dedupe", arguments={"sql": "delete from silver_pos_sales"})],
        date(2025, 3, 1),
    )
    outcome = RemediationExecutor(warehouse, settings).apply(incident)
    assert "unknown argument" in outcome.failed[0].result


def test_high_risk_actions_always_wait_for_a_human(settings, warehouse) -> None:
    settings.remediation.mode = RemediationMode.AUTO
    incident = _incident(
        [
            ProposedAction(
                name="apply_unit_correction",
                arguments={"column_name": "units_sold", "factor": 0.0833},
            )
        ],
        date(2025, 3, 1),
    )
    outcome = RemediationExecutor(warehouse, settings).apply(incident)

    assert outcome.executed == []
    assert outcome.pending[0].status is ActionStatus.AWAITING_APPROVAL
    assert incident.status is IncidentStatus.AWAITING_APPROVAL
    assert warehouse.scalar("SELECT count(*) FROM ops_correction") == 0


def test_manual_mode_holds_even_low_risk_actions(settings, warehouse) -> None:
    settings.remediation.mode = RemediationMode.MANUAL
    incident = _incident([ProposedAction(name="enable_dedupe")], date(2025, 3, 1))
    outcome = RemediationExecutor(warehouse, settings).apply(incident)

    assert outcome.executed == []
    assert outcome.pending[0].status is ActionStatus.AWAITING_APPROVAL


def test_approval_executes_and_records_the_approver(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date)
    refresh_run(business_date, warehouse, settings, "test", detect=False)

    incident = _incident(
        [
            ProposedAction(
                name="apply_unit_correction",
                arguments={"column_name": "units_sold", "factor": 0.5},
            )
        ],
        business_date,
    )
    executor = RemediationExecutor(warehouse, settings)
    executor.apply(incident)
    executor.approve(incident, 0, "alice")

    assert incident.actions[0].status is ActionStatus.EXECUTED
    assert incident.actions[0].actor == "alice"
    assert warehouse.scalar("SELECT count(*) FROM ops_correction") == 1
    assert any(event.actor == "alice" for event in incident.timeline)


def test_rejection_leaves_the_warehouse_untouched(settings, warehouse) -> None:
    incident = _incident(
        [
            ProposedAction(
                name="apply_unit_correction",
                arguments={"column_name": "units_sold", "factor": 0.5},
            )
        ],
        date(2025, 3, 1),
    )
    executor = RemediationExecutor(warehouse, settings)
    executor.apply(incident)
    executor.reject(incident, 0, "bob", "we do not rewrite reported figures")

    assert incident.actions[0].status is ActionStatus.REJECTED
    assert incident.status is IncidentStatus.DIAGNOSED
    assert warehouse.scalar("SELECT count(*) FROM ops_correction") == 0


def test_approving_an_already_executed_action_is_refused(settings, warehouse) -> None:
    incident = _incident([ProposedAction(name="enable_dedupe")], date(2025, 3, 1))
    executor = RemediationExecutor(warehouse, settings)
    executor.apply(incident)
    with pytest.raises(ValueError, match="not awaiting approval"):
        executor.approve(incident, 0, "alice")


def test_verification_reruns_and_resolves_a_fixed_incident(settings, warehouse, inject) -> None:
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("shipments", FaultType.DUPLICATE_BATCH, magnitude=0.5))
    refresh_run(business_date, warehouse, settings, "test", detect=False)

    incident = _incident(
        [ProposedAction(name="enable_dedupe"), ProposedAction(name="rerun_pipeline")],
        business_date,
        dataset="shipments",
    )
    outcome = RemediationExecutor(warehouse, settings).apply(incident)

    assert outcome.verified_healthy is True
    assert incident.status is IncidentStatus.RESOLVED
    assert any(event.event == "verification" for event in incident.timeline)


def test_verification_reports_an_unfixed_incident_as_remediated(
    settings, warehouse, inject
) -> None:
    """Containment is not a cure: a feed the warehouse cannot repair stays open."""
    business_date = date(2025, 3, 1)
    inject(business_date, FaultSpec("inventory", FaultType.NULL_SPIKE, magnitude=0.4))
    refresh_run(business_date, warehouse, settings, "test", detect=False)

    incident = _incident(
        [ProposedAction(name="quarantine_invalid_rows")], business_date, dataset="inventory"
    )
    outcome = RemediationExecutor(warehouse, settings).apply(incident)

    assert outcome.verified_healthy is False
    assert incident.status is IncidentStatus.REMEDIATED


def test_action_limit_is_enforced(settings, warehouse) -> None:
    settings.remediation.max_actions_per_incident = 1
    incident = _incident(
        [
            ProposedAction(name="enable_dedupe"),
            ProposedAction(name="quarantine_invalid_rows"),
            ProposedAction(name="rerun_pipeline"),
        ],
        date(2025, 3, 1),
    )
    RemediationExecutor(warehouse, settings).apply(incident)

    assert len(incident.actions) == 1
    assert any(event.event == "actions.truncated" for event in incident.timeline)


def test_every_whitelisted_action_declares_its_risk() -> None:
    assert ACTIONS
    for action in ACTIONS.values():
        assert isinstance(action.risk, RiskLevel)
        assert action.description
        assert set(action.required).issubset(set(action.parameters))
    assert ACTIONS["apply_unit_correction"].risk is RiskLevel.HIGH


def test_incidents_round_trip_through_the_store(settings, warehouse) -> None:
    incident = _incident([ProposedAction(name="enable_dedupe")], date(2025, 3, 1))
    incident.log_event("test", "opened", "hello")
    store = IncidentStore(warehouse)
    store.save(incident)

    loaded = store.get(incident.incident_id)
    assert loaded is not None
    assert loaded.diagnosis is not None
    assert loaded.diagnosis.root_cause is RootCause.DUPLICATE_BATCH
    assert loaded.timeline[0].detail == "hello"
    assert store.count() == 1
    assert [item.incident_id for item in store.open_incidents()] == [incident.incident_id]


def test_holds_are_scoped_and_releasable(warehouse) -> None:
    first, second = date(2025, 3, 1), date(2025, 3, 2)
    hold_id = ops.create_hold(warehouse, "pos_sales", first, "partial", "test")

    assert [hold.hold_id for hold in ops.active_holds(warehouse, first)] == [hold_id]
    assert ops.active_holds(warehouse, second) == []
    assert ops.release_hold(warehouse, hold_id) is True
    assert ops.active_holds(warehouse) == []
