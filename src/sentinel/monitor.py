"""The self-healing loop: observe, open incidents, diagnose, remediate, verify.

This is the orchestrator that ties the platform together. It is intentionally
the only module that knows about all five stages, so each stage stays testable
on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sentinel.agent.base import DiagnosisEngine, DiagnosisRequest, build_engine
from sentinel.config import Settings, get_settings
from sentinel.detection.detector import DetectionResult
from sentinel.elt.warehouse import Warehouse
from sentinel.generator.source import DATASETS
from sentinel.impact import ImpactConfigError, assess_impact, headline
from sentinel.incidents.models import Incident, IncidentStatus
from sentinel.incidents.store import IncidentStore
from sentinel.logging import get_logger
from sentinel.quality.checks import CheckResult, Severity
from sentinel.remediation.executor import RemediationExecutor, RemediationOutcome
from sentinel.runner import RunObservation, refresh_run

log = get_logger(__name__)

#: A partition below this share of its expected row count is treated as critical
#: even when every structural check passes - half a day of sales is not a warning.
CRITICAL_VOLUME_RATIO = 0.5


@dataclass
class MonitorResult:
    """Everything that happened during one monitored business date."""

    observation: RunObservation
    incidents: list[Incident] = field(default_factory=list)
    outcomes: list[RemediationOutcome] = field(default_factory=list)

    @property
    def business_date(self) -> date:
        """The date that was monitored."""
        return self.observation.business_date

    @property
    def resolved(self) -> list[Incident]:
        """Incidents that were fixed and verified within this run."""
        return [
            incident for incident in self.incidents if incident.status is IncidentStatus.RESOLVED
        ]

    @property
    def awaiting_approval(self) -> list[Incident]:
        """Incidents holding a proposed action that needs a human."""
        return [
            incident
            for incident in self.incidents
            if incident.status is IncidentStatus.AWAITING_APPROVAL
        ]


def monitor(
    business_date: date,
    warehouse: Warehouse,
    settings: Settings | None = None,
    triggered_by: str = "cli",
    diagnose: bool = True,
    remediate: bool = True,
    engine: DiagnosisEngine | None = None,
) -> MonitorResult:
    """Run, observe, and heal one business date.

    Args:
        business_date: Date to process.
        warehouse: Open warehouse.
        settings: Settings override.
        triggered_by: Provenance recorded on the pipeline run.
        diagnose: Whether to ask an engine for a root cause.
        remediate: Whether to apply the resulting actions (implies ``diagnose``).
        engine: Engine override; by default one is chosen from settings.

    Returns:
        A :class:`MonitorResult` describing the run and every incident opened.
    """
    settings = settings or get_settings()
    observation = refresh_run(business_date, warehouse, settings, triggered_by)
    result = MonitorResult(observation=observation)

    store = IncidentStore(warehouse)
    executor = RemediationExecutor(warehouse, settings)
    resolved_engine = engine or (build_engine(settings) if diagnose else None)

    for dataset in DATASETS:
        incident = _open_incident(observation, dataset)
        if incident is None:
            continue
        _assess(incident, settings)
        store.save(incident)
        result.incidents.append(incident)

        if resolved_engine is None:
            continue

        incident.diagnosis = resolved_engine.diagnose(
            DiagnosisRequest(incident=incident, warehouse=warehouse, settings=settings)
        )
        incident.status = IncidentStatus.DIAGNOSED
        incident.log_event(
            incident.diagnosis.engine,
            "diagnosed",
            f"{incident.diagnosis.root_cause}: {incident.diagnosis.summary}",
        )
        store.save(incident)

        if not remediate:
            continue

        outcome = executor.apply(incident)
        result.outcomes.append(outcome)
        store.save(incident)

    log.info(
        "monitor.finished",
        business_date=str(business_date),
        incidents=len(result.incidents),
        resolved=len(result.resolved),
        awaiting_approval=len(result.awaiting_approval),
    )
    return result


def _open_incident(observation: RunObservation, dataset: str) -> Incident | None:
    """Create an incident for a dataset if this run warrants one."""
    failed = observation.failing_checks(dataset)
    detection = observation.detections.get(dataset)
    anomalous = detection.is_anomalous if detection else False
    if not failed and not anomalous:
        return None

    severity = _severity(failed, detection)
    incident = Incident(
        run_id=observation.pipeline.run_id,
        business_date=observation.business_date,
        dataset=dataset,
        severity=severity,
        title=_title(dataset, failed, detection),
        failed_checks=[check.as_dict() for check in failed],
        anomalies=[signal.as_dict() for signal in detection.signals] if detection else [],
        evidence=_evidence(observation, dataset, detection),
    )
    incident.log_event("monitor", "opened", incident.title)
    log.warning(
        "incident.opened",
        incident_id=incident.incident_id,
        dataset=dataset,
        severity=severity,
        failed_checks=[check.check_name for check in failed],
    )
    return incident


def _assess(incident: Incident, settings: Settings) -> None:
    """Attach blast radius and estimated loss, before anyone diagnoses.

    A broken lineage file must not stop incidents from being diagnosed and
    repaired, so it costs the incident its impact section and nothing else. The
    timeline says why, where an operator will look.
    """
    if not settings.impact.enabled:
        return
    try:
        incident.impact = assess_impact(incident, settings)
    except ImpactConfigError as error:
        log.warning("impact.unavailable", incident_id=incident.incident_id, error=str(error))
        incident.log_event("monitor", "impact.unavailable", str(error))
        return
    incident.log_event("monitor", "impact.assessed", headline(incident.impact))


def _severity(failed: list[CheckResult], detection: DetectionResult | None) -> str:
    """Critical when data is structurally broken or volume collapsed."""
    if any(check.severity is Severity.CRITICAL for check in failed):
        return "critical"
    if detection:
        for signal in detection.signals:
            if signal.metric == "row_count" and signal.ratio < CRITICAL_VOLUME_RATIO:
                return "critical"
    return "warning"


def _title(dataset: str, failed: list[CheckResult], detection: DetectionResult | None) -> str:
    """A one-line human summary of why the incident exists."""
    if failed:
        names = ", ".join(check.check_name for check in failed[:3])
        return f"{dataset}: {len(failed)} quality check(s) failed ({names})"
    if detection and detection.signals:
        return f"{dataset}: {detection.signals[0].describe()}"
    return f"{dataset}: multivariate anomaly detected in the metric vector"


def _evidence(
    observation: RunObservation, dataset: str, detection: DetectionResult | None
) -> dict[str, object]:
    """Assemble the context bundle attached to an incident."""
    load = observation.pipeline.bronze.get(dataset)
    build = observation.pipeline.silver.get(dataset)
    return {
        "run_id": observation.pipeline.run_id,
        "pipeline_status": observation.pipeline.status,
        "gold_published": observation.pipeline.gold_published,
        "extract_arrived": load.arrived if load else False,
        "columns_in_extract": load.payload_columns if load else [],
        "expected_columns": list(DATASETS[dataset].required_columns),
        "rows_loaded": load.rows if load else 0,
        "silver_rows_for_date": build.rows_for_date if build else 0,
        "rejected_rows_total": build.rejected_rows if build else 0,
        "detector_history_runs": detection.history_runs if detection else 0,
        "detector_skipped": detection.skipped_reason if detection else None,
        "metrics": observation.metrics.get(dataset, {}),
    }
