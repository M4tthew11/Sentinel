"""HTTP surface of the platform.

Read endpoints answer "what happened and why"; two write endpoints implement the
human half of human-in-the-loop remediation. A DuckDB connection is not safe to
share across threads, so every request opens its own - cheap for an embedded
engine, and the pattern maps directly onto a connection pool if the warehouse
moves to Databricks or Postgres.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from sentinel.api.auth import authenticated_approver
from sentinel.api.preview import preview
from sentinel.config import Settings, get_settings
from sentinel.elt import ops
from sentinel.elt.warehouse import Warehouse
from sentinel.evals.reports import EVAL_REPORT_DIR, load_reports
from sentinel.incidents.models import Incident, IncidentStatus
from sentinel.incidents.store import IncidentStore
from sentinel.logging import get_logger
from sentinel.remediation.actions import ACTIONS
from sentinel.remediation.executor import RemediationExecutor

log = get_logger(__name__)

app = FastAPI(
    title="Sentinel",
    version="0.1.0",
    summary="AI-powered self-healing data platform",
    description=(
        "Incidents, pipeline metrics and the approval workflow for agent-proposed remediations."
    ),
)


def _warehouse(settings: Settings | None = None, read_only: bool = True) -> Warehouse:
    """Open a per-request warehouse connection."""
    settings = settings or get_settings()
    if not settings.warehouse_path.exists():
        raise HTTPException(
            status_code=503,
            detail="No warehouse yet. Run 'sentinel demo' to build one.",
        )
    return Warehouse(settings.warehouse_path, read_only=read_only)


# --------------------------------------------------------------------------
# Health and summary
# --------------------------------------------------------------------------


@app.get("/health", tags=["ops"])
def health() -> dict[str, Any]:
    """Liveness probe: reports whether a warehouse is present."""
    settings = get_settings()
    return {
        "status": "ok",
        "warehouse": str(settings.warehouse_path),
        "warehouse_ready": settings.warehouse_path.exists(),
    }


@app.get("/api/summary", tags=["ops"])
def summary() -> dict[str, Any]:
    """Headline counters for the dashboard."""
    warehouse = _warehouse()
    try:
        store = IncidentStore(warehouse)
        incidents = store.search(limit=500)
        by_status: dict[str, int] = {}
        for incident in incidents:
            by_status[str(incident.status)] = by_status.get(str(incident.status), 0) + 1
        latest_run = warehouse.fetch_one(
            """
            SELECT run_id, business_date, status, started_at
            FROM ops_pipeline_run
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
        return {
            "incidents_total": len(incidents),
            "incidents_by_status": by_status,
            "open_incidents": len(store.open_incidents()),
            "active_holds": [
                {
                    "hold_id": hold.hold_id,
                    "dataset": hold.dataset,
                    "business_date": hold.business_date.isoformat(),
                    "reason": hold.reason,
                }
                for hold in ops.active_holds(warehouse)
            ],
            "tickets": int(warehouse.scalar("SELECT count(*) FROM ops_ticket") or 0),
            "latest_run": None
            if latest_run is None
            else {
                "run_id": latest_run[0],
                "business_date": str(latest_run[1]),
                "status": latest_run[2],
            },
        }
    finally:
        warehouse.close()


# --------------------------------------------------------------------------
# Incidents
# --------------------------------------------------------------------------


@app.get("/api/incidents", tags=["incidents"])
def list_incidents(
    business_date: date | None = Query(default=None, description="Filter by business date."),
    dataset: str | None = Query(default=None, description="Filter by dataset."),
    status: IncidentStatus | None = Query(default=None, description="Filter by status."),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List incidents, newest first."""
    warehouse = _warehouse()
    try:
        incidents = IncidentStore(warehouse).search(
            business_date=business_date, dataset=dataset, status=status, limit=limit
        )
        return [_serialise(incident) for incident in incidents]
    finally:
        warehouse.close()


@app.get("/api/incidents/{incident_id}", tags=["incidents"])
def get_incident(incident_id: str) -> dict[str, Any]:
    """Fetch a single incident document in full."""
    warehouse = _warehouse()
    try:
        incident = IncidentStore(warehouse).get(incident_id)
    finally:
        warehouse.close()
    if incident is None:
        raise HTTPException(status_code=404, detail=f"No incident {incident_id}")
    return incident.model_dump(mode="json")


def _approver_name(authenticated: str | None, claimed: str | None) -> str:
    """The name recorded on the incident for an approval or rejection.

    A token-authenticated caller is recorded under the name their token belongs
    to; whatever name the request body carries is ignored. Only an unauthenticated
    local caller (loopback, no tokens configured) may name themselves.
    """
    return authenticated or claimed or "local-operator"


@app.post("/api/incidents/{incident_id}/actions/{action_index}/approve", tags=["remediation"])
def approve_action(
    incident_id: str,
    action_index: int,
    approver: str | None = Body(default=None, embed=True),
    authenticated: str | None = Depends(authenticated_approver),
) -> dict[str, Any]:
    """Approve a pending high-risk action and execute it."""
    approver = _approver_name(authenticated, approver)
    warehouse = _warehouse(read_only=False)
    try:
        store = IncidentStore(warehouse)
        incident = store.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail=f"No incident {incident_id}")
        if action_index >= len(incident.actions):
            raise HTTPException(status_code=404, detail=f"No action {action_index}")
        try:
            outcome = RemediationExecutor(warehouse, get_settings()).approve(
                incident, action_index, approver
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        store.save(outcome.incident)
        return _serialise(outcome.incident)
    finally:
        warehouse.close()


@app.post("/api/incidents/{incident_id}/actions/{action_index}/reject", tags=["remediation"])
def reject_action(
    incident_id: str,
    action_index: int,
    approver: str | None = Body(default=None, embed=True),
    reason: str = Body(default="rejected by operator", embed=True),
    authenticated: str | None = Depends(authenticated_approver),
) -> dict[str, Any]:
    """Reject a pending action."""
    approver = _approver_name(authenticated, approver)
    warehouse = _warehouse(read_only=False)
    try:
        store = IncidentStore(warehouse)
        incident = store.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail=f"No incident {incident_id}")
        if action_index >= len(incident.actions):
            raise HTTPException(status_code=404, detail=f"No action {action_index}")
        RemediationExecutor(warehouse, get_settings()).reject(
            incident, action_index, approver, reason
        )
        store.save(incident)
        return _serialise(incident)
    finally:
        warehouse.close()


# --------------------------------------------------------------------------
# Metrics and data
# --------------------------------------------------------------------------


@app.get("/api/metrics", tags=["metrics"])
def metrics(
    dataset: str = Query(description="Dataset name."),
    metric: str = Query(default="row_count", description="Metric name."),
    days: int = Query(default=30, ge=1, le=365),
    until: date | None = Query(
        default=None, description="Last business date to include; defaults to the latest."
    ),
) -> dict[str, Any]:
    """Return a metric time series for charting, oldest first.

    A rebuilt date (a remediation reruns the pipeline) holds the latest
    measurement; what detection originally saw is on the incident's anomaly
    signals.
    """
    warehouse = _warehouse()
    try:
        rows = warehouse.fetch_records(
            """
            SELECT business_date, value
            FROM dq_metric
            WHERE dataset = ? AND metric = ? AND (? IS NULL OR business_date <= ?)
            ORDER BY business_date DESC
            LIMIT ?
            """,
            [dataset, metric, until, until, days],
        )
    finally:
        warehouse.close()
    points = [
        {"business_date": str(row["business_date"].date()), "value": row["value"]} for row in rows
    ]
    points.reverse()
    return {"dataset": dataset, "metric": metric, "points": points}


@app.get("/api/incidents/{incident_id}/actions/{action_index}/preview", tags=["remediation"])
def preview_action(incident_id: str, action_index: int) -> dict[str, Any]:
    """Describe what an action would change, before anyone approves it."""
    warehouse = _warehouse()
    try:
        incident = IncidentStore(warehouse).get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail=f"No incident {incident_id}")
        if not 0 <= action_index < len(incident.actions):
            raise HTTPException(status_code=404, detail=f"No action {action_index}")
        return preview(warehouse, incident, incident.actions[action_index])
    finally:
        warehouse.close()


@app.get("/api/evals", tags=["evals"])
def eval_reports() -> list[dict[str, Any]]:
    """The latest saved report per scenario and engine, written by ``sentinel eval``."""
    return load_reports(get_settings().data_dir / EVAL_REPORT_DIR)


@app.get("/api/actions", tags=["remediation"])
def action_catalogue() -> list[dict[str, Any]]:
    """The remediation whitelist, as served to the agent."""
    return [
        {
            "name": action.name,
            "description": action.description,
            "risk": str(action.risk),
            "required": list(action.required),
            "parameters": sorted(action.parameters),
        }
        for action in ACTIONS.values()
    ]


@app.get("/api/gold/brand-daily", tags=["metrics"])
def brand_daily(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
    """Recent rows of the published business layer."""
    warehouse = _warehouse()
    try:
        if not warehouse.table_exists("gold_brand_daily"):
            return []
        rows = warehouse.fetch_records(
            """
            SELECT business_date, brand, category, units_sold, net_sales_usd,
                   cases_shipped, on_hand_cases
            FROM gold_brand_daily
            ORDER BY business_date DESC, brand
            LIMIT ?
            """,
            [limit],
        )
    finally:
        warehouse.close()
    return [
        {
            "business_date": str(row["business_date"].date()),
            "brand": str(row["brand"]),
            "category": str(row["category"]),
            "units_sold": int(row["units_sold"] or 0),
            "net_sales_usd": float(row["net_sales_usd"] or 0.0),
            "cases_shipped": int(row["cases_shipped"] or 0),
            "on_hand_cases": int(row["on_hand_cases"] or 0),
        }
        for row in rows
    ]


def _serialise(incident: Incident) -> dict[str, Any]:
    """Compact incident representation for list views."""
    diagnosis = incident.diagnosis
    return {
        "incident_id": incident.incident_id,
        "business_date": incident.business_date.isoformat(),
        "dataset": incident.dataset,
        "severity": incident.severity,
        "status": str(incident.status),
        "title": incident.title,
        "root_cause": str(diagnosis.root_cause) if diagnosis else None,
        "confidence": diagnosis.confidence if diagnosis else None,
        "engine": diagnosis.engine if diagnosis else None,
        "summary": diagnosis.summary if diagnosis else "",
        "actions": [
            {
                "index": index,
                "name": action.name,
                "risk": str(action.risk),
                "status": str(action.status),
                "result": action.result,
                "rationale": action.rationale,
            }
            for index, action in enumerate(incident.actions)
        ],
    }


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    """A dependency-free incident dashboard."""
    from sentinel.api.dashboard import DASHBOARD_HTML

    return DASHBOARD_HTML
