"""The HTTP surface, including the human half of the approval workflow."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from sentinel.api import main as api
from sentinel.generator.faults import FaultSpec, FaultType
from sentinel.incidents.models import ActionStatus, IncidentStatus
from sentinel.incidents.store import IncidentStore
from sentinel.monitor import monitor


@pytest.fixture
def client(settings, monkeypatch) -> TestClient:
    """A test client bound to the isolated settings."""
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    return TestClient(api.app)


@pytest.fixture
def with_incident(settings, seeded, next_day, inject):
    """A warehouse holding one incident that needs human approval."""
    inject(
        next_day,
        FaultSpec(
            "pos_sales",
            FaultType.UNIT_OF_MEASURE,
            column="units_sold",
            metadata={"factor": "12"},
        ),
    )
    result = monitor(next_day, seeded, settings, "test")
    seeded.close()
    return result


def test_health_reports_the_warehouse(client, settings) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_endpoints_explain_a_missing_warehouse(client, settings) -> None:
    settings.warehouse_path.unlink(missing_ok=True)
    response = client.get("/api/incidents")
    assert response.status_code == 503
    assert "sentinel demo" in response.json()["detail"]


def test_incident_list_and_detail(client, with_incident) -> None:
    listed = client.get("/api/incidents").json()
    assert listed
    incident_id = listed[0]["incident_id"]
    assert listed[0]["root_cause"] == "unit_of_measure"

    detail = client.get(f"/api/incidents/{incident_id}").json()
    assert detail["incident_id"] == incident_id
    assert detail["diagnosis"]["reasoning"]
    assert detail["evidence"]["metrics"]


def test_unknown_incident_is_a_404(client, with_incident) -> None:
    assert client.get("/api/incidents/INC-nope").status_code == 404


def test_filters_narrow_the_list(client, with_incident, next_day) -> None:
    assert client.get("/api/incidents", params={"dataset": "shipments"}).json() == []
    matched = client.get("/api/incidents", params={"business_date": next_day.isoformat()}).json()
    assert len(matched) == 1


def test_approval_executes_a_pending_action(client, settings, with_incident) -> None:
    incident_id = client.get("/api/incidents").json()[0]["incident_id"]
    detail = client.get(f"/api/incidents/{incident_id}").json()
    index = next(
        position
        for position, action in enumerate(detail["actions"])
        if action["status"] == "awaiting_approval"
    )

    response = client.post(
        f"/api/incidents/{incident_id}/actions/{index}/approve",
        json={"approver": "tester"},
    )
    assert response.status_code == 200
    assert response.json()["actions"][index]["status"] == str(ActionStatus.EXECUTED)

    from sentinel.elt.warehouse import Warehouse

    warehouse = Warehouse(settings.warehouse_path)
    try:
        assert warehouse.scalar("SELECT count(*) FROM ops_correction") == 1
        stored = IncidentStore(warehouse).get(incident_id)
        assert stored is not None
        assert stored.status is not IncidentStatus.AWAITING_APPROVAL
    finally:
        warehouse.close()


def test_rejecting_twice_is_a_conflict(client, with_incident) -> None:
    incident_id = client.get("/api/incidents").json()[0]["incident_id"]
    detail = client.get(f"/api/incidents/{incident_id}").json()
    index = next(
        position
        for position, action in enumerate(detail["actions"])
        if action["status"] == "awaiting_approval"
    )

    assert (
        client.post(
            f"/api/incidents/{incident_id}/actions/{index}/reject",
            json={"approver": "tester", "reason": "no"},
        ).status_code
        == 200
    )
    conflict = client.post(
        f"/api/incidents/{incident_id}/actions/{index}/approve", json={"approver": "tester"}
    )
    assert conflict.status_code == 409


def test_summary_counts_incidents_and_holds(client, with_incident) -> None:
    summary = client.get("/api/summary").json()
    assert summary["incidents_total"] >= 1
    assert summary["latest_run"]["status"] in {"succeeded", "held"}


def test_metrics_series_is_ordered_oldest_first(client, with_incident) -> None:
    payload = client.get(
        "/api/metrics", params={"dataset": "pos_sales", "metric": "row_count", "days": 10}
    ).json()
    dates = [point["business_date"] for point in payload["points"]]
    assert dates == sorted(dates)
    assert len(dates) <= 10


def test_action_catalogue_is_published(client, with_incident) -> None:
    actions = {action["name"]: action for action in client.get("/api/actions").json()}
    assert actions["apply_unit_correction"]["risk"] == "high"
    assert "enable_dedupe" in actions


def test_gold_endpoint_returns_business_rows(client, with_incident) -> None:
    rows = client.get("/api/gold/brand-daily", params={"limit": 5}).json()
    assert rows
    assert {"business_date", "brand", "units_sold"} <= set(rows[0])


def test_dashboard_is_self_contained(client, with_incident) -> None:
    body = client.get("/").text
    assert "<title>Sentinel" in body
    assert "http://" not in body.split("</style>")[0], "no external stylesheets"
    assert "/api/incidents" in body


def test_openapi_documents_the_service(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/incidents" in schema["paths"]
    assert schema["info"]["title"] == "Sentinel"


def test_business_date_is_validated(client, with_incident) -> None:
    assert client.get("/api/incidents", params={"business_date": "not-a-date"}).status_code == 422


def test_incidents_are_serialised_with_dates_as_strings(client, with_incident) -> None:
    payload = client.get("/api/incidents").json()[0]
    assert payload["business_date"] == str(date.fromisoformat(payload["business_date"]))
