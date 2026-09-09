"""Applies an agent's proposed actions, then verifies its own work.

The executor is where autonomy is actually bounded:

* only names in :data:`sentinel.remediation.actions.ACTIONS` can run;
* arguments are validated before anything touches the warehouse;
* ``RiskLevel.HIGH`` actions never auto-execute, whatever the settings say;
* after applying changes it re-runs the pipeline and re-checks the data, so an
  incident is only closed when the evidence says it is fixed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sentinel.config import RemediationMode, Settings, get_settings
from sentinel.elt.warehouse import Warehouse
from sentinel.incidents.models import (
    ActionRecord,
    ActionStatus,
    Incident,
    IncidentStatus,
    RiskLevel,
)
from sentinel.logging import get_logger
from sentinel.remediation.actions import ACTIONS, ActionContext
from sentinel.runner import refresh_run

log = get_logger(__name__)


@dataclass
class RemediationOutcome:
    """What the executor did to an incident."""

    incident: Incident
    executed: list[ActionRecord]
    pending: list[ActionRecord]
    failed: list[ActionRecord]
    verified_healthy: bool | None = None
    """``None`` when no verification run was needed."""


class RemediationExecutor:
    """Executes whitelisted actions on behalf of an incident."""

    def __init__(
        self,
        warehouse: Warehouse,
        settings: Settings | None = None,
        actor: str = "sentinel-agent",
    ) -> None:
        self.warehouse = warehouse
        self.settings = settings or get_settings()
        self.actor = actor

    # -- public API --------------------------------------------------------

    def apply(self, incident: Incident) -> RemediationOutcome:
        """Apply the diagnosis's proposed actions and verify the result."""
        if incident.diagnosis is None:
            return RemediationOutcome(incident, [], [], [])

        limit = self.settings.remediation.max_actions_per_incident
        proposals = incident.diagnosis.proposed_actions[:limit]
        if len(incident.diagnosis.proposed_actions) > limit:
            incident.log_event(
                self.actor,
                "actions.truncated",
                f"{len(incident.diagnosis.proposed_actions)} proposed, {limit} allowed",
            )

        records = [self._apply_one(incident, index) for index in range(len(proposals))]
        incident.actions.extend(records)
        return self._finalise(incident, records)

    def approve(self, incident: Incident, action_index: int, approver: str) -> RemediationOutcome:
        """Execute an action a human has approved."""
        record = incident.actions[action_index]
        if record.status is not ActionStatus.AWAITING_APPROVAL:
            raise ValueError(f"action {action_index} is {record.status}, not awaiting approval")

        definition = ACTIONS[record.name]
        try:
            result = definition.handler(self._context(incident), dict(record.arguments))
        except Exception as error:
            record.status = ActionStatus.FAILED
            record.result = f"{type(error).__name__}: {error}"
            incident.log_event(approver, "action.failed", f"{record.name}: {record.result}")
            return self._finalise(incident, [record])

        record.status = ActionStatus.EXECUTED
        record.result = result
        record.actor = approver
        incident.log_event(approver, "action.approved", f"{record.name}: {result}")
        return self._finalise(incident, [record])

    def reject(self, incident: Incident, action_index: int, approver: str, reason: str) -> Incident:
        """Reject a pending action."""
        record = incident.actions[action_index]
        record.status = ActionStatus.REJECTED
        record.result = reason
        record.actor = approver
        incident.log_event(approver, "action.rejected", f"{record.name}: {reason}")
        if not any(action.status is ActionStatus.AWAITING_APPROVAL for action in incident.actions):
            incident.status = IncidentStatus.DIAGNOSED
        return incident

    # -- internals ---------------------------------------------------------

    def _context(self, incident: Incident) -> ActionContext:
        return ActionContext(
            warehouse=self.warehouse,
            incident=incident,
            settings=self.settings,
            actor=self.actor,
        )

    def _apply_one(self, incident: Incident, index: int) -> ActionRecord:
        assert incident.diagnosis is not None
        proposal = incident.diagnosis.proposed_actions[index]
        definition = ACTIONS.get(proposal.name)

        if definition is None:
            incident.log_event(self.actor, "action.rejected", f"unknown action {proposal.name!r}")
            return ActionRecord(
                name=proposal.name,
                arguments=proposal.arguments,
                rationale=proposal.rationale,
                status=ActionStatus.FAILED,
                result=f"{proposal.name!r} is not in the remediation whitelist",
                actor=self.actor,
            )

        record = ActionRecord(
            name=definition.name,
            arguments=dict(proposal.arguments),
            rationale=proposal.rationale,
            risk=definition.risk,
            actor=self.actor,
        )

        try:
            definition.validate_arguments(record.arguments)
        except ValueError as error:
            record.status = ActionStatus.FAILED
            record.result = str(error)
            incident.log_event(self.actor, "action.invalid", f"{definition.name}: {error}")
            return record

        if not self._may_auto_execute(definition.risk):
            record.status = ActionStatus.AWAITING_APPROVAL
            record.result = f"risk={definition.risk} requires human approval"
            incident.log_event(
                self.actor, "action.awaiting_approval", f"{definition.name}: {record.result}"
            )
            return record

        try:
            record.result = definition.handler(self._context(incident), record.arguments)
            record.status = ActionStatus.EXECUTED
            incident.log_event(self.actor, "action.executed", f"{definition.name}: {record.result}")
        except Exception as error:
            record.status = ActionStatus.FAILED
            record.result = f"{type(error).__name__}: {error}"
            incident.log_event(self.actor, "action.failed", f"{definition.name}: {record.result}")
        return record

    def _may_auto_execute(self, risk: RiskLevel) -> bool:
        """High-risk actions always need a human, regardless of the configured mode."""
        if risk is RiskLevel.HIGH:
            return False
        return self.settings.remediation.mode is RemediationMode.AUTO

    def _finalise(self, incident: Incident, records: list[ActionRecord]) -> RemediationOutcome:
        executed = [r for r in records if r.status is ActionStatus.EXECUTED]
        pending = [r for r in incident.actions if r.status is ActionStatus.AWAITING_APPROVAL]
        failed = [r for r in records if r.status is ActionStatus.FAILED]

        verified: bool | None = None
        if any(ACTIONS[r.name].triggers_rebuild for r in executed if r.name in ACTIONS):
            verified = self._verify(incident, incident.business_date)

        if pending:
            incident.status = IncidentStatus.AWAITING_APPROVAL
        elif verified is True:
            incident.status = IncidentStatus.RESOLVED
        elif executed:
            incident.status = IncidentStatus.REMEDIATED
        elif incident.diagnosis is not None:
            incident.status = IncidentStatus.DIAGNOSED

        return RemediationOutcome(
            incident=incident,
            executed=executed,
            pending=pending,
            failed=failed,
            verified_healthy=verified,
        )

    def _verify(self, incident: Incident, business_date: date) -> bool:
        """Re-run and re-check the business date; returns whether it is now clean."""
        observation = refresh_run(
            business_date,
            self.warehouse,
            self.settings,
            triggered_by=f"remediation:{incident.incident_id}",
            detect=False,
        )
        remaining = observation.failing_checks(incident.dataset)
        healthy = not observation.has_critical_failure(incident.dataset)
        incident.log_event(
            self.actor,
            "verification",
            (
                "all critical checks pass after remediation"
                if healthy
                else f"{len(remaining)} check(s) still failing: "
                + ", ".join(result.check_name for result in remaining)
            ),
        )
        log.info(
            "remediation.verified",
            incident_id=incident.incident_id,
            dataset=incident.dataset,
            healthy=healthy,
        )
        return healthy
