"""Applies an agent's proposed actions, then verifies its own work.

The executor is where autonomy is actually bounded:

* only names in :data:`sentinel.remediation.actions.ACTIONS` can run;
* arguments are validated before anything touches the warehouse;
* hard rules send an action to a human whatever anyone says: ``RiskLevel.HIGH``,
  or a target outside the incident's dataset and date
  (:mod:`sentinel.remediation.policy`);
* a reviewer gives every other action an independent second opinion, and only
  what it approves runs unattended - no verdict, for any reason, means a human;
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
    ActionReview,
    ActionStatus,
    Incident,
    IncidentStatus,
    ProposedAction,
    ReviewVerdict,
)
from sentinel.logging import get_logger
from sentinel.quality.metrics import ROW_COUNT
from sentinel.remediation.actions import ACTIONS, ActionContext
from sentinel.remediation.policy import hard_rules
from sentinel.review.base import ActionUnderReview, Reviewer, ReviewRequest, build_reviewer
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
        reviewer: Reviewer | None = None,
    ) -> None:
        self.warehouse = warehouse
        self.settings = settings or get_settings()
        self.actor = actor
        self._reviewer = reviewer
        self._reviewer_built = reviewer is not None

    @property
    def reviewer(self) -> Reviewer | None:
        """The configured reviewer, built on first use; ``None`` when reviewing is off."""
        if not self._reviewer_built:
            self._reviewer = build_reviewer(self.settings)
            self._reviewer_built = True
        return self._reviewer

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

        records = self.plan(incident, proposals)
        for record in records:
            if record.status is ActionStatus.PROPOSED:
                self._execute(incident, record)
        incident.actions.extend(records)
        return self._finalise(incident, records)

    def plan(self, incident: Incident, proposals: list[ProposedAction]) -> list[ActionRecord]:
        """Decide what may run unattended, without running anything.

        Each returned record is ``FAILED`` (not on the whitelist, or bad
        arguments), ``AWAITING_APPROVAL`` (a human must decide; ``result`` says
        why) or still ``PROPOSED`` (cleared to execute).
        """
        records = [self._validate(incident, proposal) for proposal in proposals]
        candidates = [record for record in records if record.status is ActionStatus.PROPOSED]
        self._review(incident, candidates)

        for record in candidates:
            record.hard_rules = hard_rules(ACTIONS[record.name], record.arguments, incident)
            reason = self._needs_human(record)
            if reason is None:
                continue
            record.status = ActionStatus.AWAITING_APPROVAL
            record.result = reason
            incident.log_event(self.actor, "action.awaiting_approval", f"{record.name}: {reason}")
        return records

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

    def _validate(self, incident: Incident, proposal: ProposedAction) -> ActionRecord:
        """Check a proposal against the whitelist and its argument schema."""
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

    def _review(self, incident: Incident, records: list[ActionRecord]) -> None:
        """Attach the reviewer's opinion to each record, failing closed.

        The reviewer sees the incident and the bare actions. Whatever goes wrong -
        an exception, a short answer - leaves the affected actions escalated: the
        absence of an approval is never treated as one.
        """
        reviewer = self.reviewer
        if reviewer is None or not records:
            return

        request = ReviewRequest(
            incident=incident,
            actions=[
                ActionUnderReview.from_proposal(record.name, record.arguments) for record in records
            ],
            warehouse=self.warehouse,
            settings=self.settings,
        )
        try:
            reviews = reviewer.review(request)
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            log.warning("review.failed", incident_id=incident.incident_id, error=detail)
            incident.log_event(reviewer.name, "review.failed", detail)
            reviews = []

        for index, record in enumerate(records):
            record.review = (
                reviews[index]
                if index < len(reviews)
                else ActionReview(
                    verdict=ReviewVerdict.ESCALATE,
                    reason="the reviewer failed to return a verdict",
                    reviewer=reviewer.name,
                )
            )
            incident.log_event(
                record.review.reviewer,
                "action.reviewed",
                f"{record.name}: {record.review.verdict} - {record.review.reason}",
            )

    def _needs_human(self, record: ActionRecord) -> str | None:
        """Why this action must wait for a human, or ``None`` when it may run.

        Checked in order of authority: the hard rules, then the configured mode,
        then the reviewer. A reviewer's approval is the last gate, never a way
        around the first two.
        """
        if record.hard_rules:
            return f"{'; '.join(record.hard_rules)} requires human approval"
        if self.settings.remediation.mode is not RemediationMode.AUTO:
            return f"risk={record.risk} requires human approval"
        if record.review is not None and record.review.verdict is not ReviewVerdict.APPROVE:
            return f"reviewer escalated: {record.review.reason}"
        return None

    def _execute(self, incident: Incident, record: ActionRecord) -> None:
        definition = ACTIONS[record.name]
        try:
            record.result = definition.handler(self._context(incident), record.arguments)
            record.status = ActionStatus.EXECUTED
            incident.log_event(self.actor, "action.executed", f"{definition.name}: {record.result}")
        except Exception as error:
            record.status = ActionStatus.FAILED
            record.result = f"{type(error).__name__}: {error}"
            incident.log_event(self.actor, "action.failed", f"{definition.name}: {record.result}")

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
        """Re-run and re-check the business date; returns whether it is now clean.

        Clean means both halves of detection agree: no critical check fails *and*
        the metrics are back inside their historical baseline. Checking only the
        contracts would call a partition "resolved" after deduplicating it while a
        unit change on the same rows is still inflating every figure, because no
        contract describes what a unit change looks like.
        """
        observation = refresh_run(
            business_date,
            self.warehouse,
            self.settings,
            triggered_by=f"remediation:{incident.incident_id}",
        )
        remaining = observation.failing_checks(incident.dataset)
        contracts_hold = not observation.has_critical_failure(incident.dataset)
        detection = observation.detections.get(incident.dataset)
        # Row count measures what was *delivered*; no warehouse-side fix changes
        # it, so a duplicated file stays "too many rows" in bronze forever. Only
        # the value metrics, built from silver, say whether the fix worked.
        drifting = (
            [signal.metric for signal in detection.signals if signal.metric != ROW_COUNT]
            if detection
            else []
        )
        healthy = contracts_hold and not drifting

        if healthy:
            detail = "all critical checks pass and metrics are back within baseline"
        elif not contracts_hold:
            detail = f"{len(remaining)} check(s) still failing: " + ", ".join(
                result.check_name for result in remaining
            )
        else:
            detail = "checks pass but metrics are still anomalous: " + ", ".join(drifting)
        incident.log_event(self.actor, "verification", detail)
        log.info(
            "remediation.verified",
            incident_id=incident.incident_id,
            dataset=incident.dataset,
            healthy=healthy,
            anomalous_metrics=drifting,
        )
        return healthy
