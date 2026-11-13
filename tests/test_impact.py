"""Blast radius and estimated loss: the lineage graph, the cost model, and where they show up."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from sentinel.agent.prompts import incident_brief
from sentinel.api import main as api
from sentinel.api.auth import APPROVER_TOKENS_ENV
from sentinel.cli import app
from sentinel.config import Settings, reset_settings_cache
from sentinel.generator.faults import FaultSpec, FaultType
from sentinel.generator.source import DATASETS
from sentinel.impact import (
    ImpactConfigError,
    assess_impact,
    headline,
    load_lineage,
    records_at_risk,
)
from sentinel.incidents.models import Incident
from sentinel.incidents.store import IncidentStore
from sentinel.monitor import monitor
from sentinel.review.base import ActionUnderReview
from sentinel.review.prompts import review_brief

SMALL = """
sources: [orders, stock]
assets:
  silver_orders: {kind: table, depends_on: [orders]}
  orders_report:
    kind: report
    name: Orders report
    owner: Sales
    depends_on: [silver_orders]
  pricing:
    kind: decision
    name: Price changes
    criticality: critical
    depends_on: [orders_report]
  board_pack:
    kind: report
    depends_on: [orders_report, silver_orders]
    cost_per_hour: 500
  stock_dashboard: {kind: dashboard, depends_on: [stock]}
cost_model:
  currency: EUR
  exposure_hours: 4
  cost_per_hour_by_kind: {table: 0, model: 0, report: 10, dashboard: 20, decision: 100}
  criticality_multiplier: {low: 0.5, medium: 1, high: 2, critical: 3}
  severity_factor: {critical: 1.0, warning: 0.5}
  cost_per_consumer: 25
  cost_per_record: {default: 0.1, stock: 1}
  uncertainty: 0.25
  priority_thresholds: {p1: 3000, p2: 1000}
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "impact.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _incident(dataset: str = "orders", **overrides: Any) -> Incident:
    fields: dict[str, Any] = {
        "run_id": "RUN-test",
        "business_date": date(2025, 3, 17),
        "dataset": dataset,
        "severity": "critical",
        "title": "test incident",
        "evidence": {"rows_loaded": 1000},
    }
    fields.update(overrides)
    return Incident(**fields)


def _settings_with(settings: Settings, path: Path) -> Settings:
    settings.impact.config_path = path
    return settings


# -- lineage -----------------------------------------------------------------


def test_built_in_lineage_covers_every_dataset() -> None:
    graph = load_lineage()

    assert sorted(graph.config.sources) == sorted(DATASETS)
    for dataset in DATASETS:
        assert graph.downstream(dataset), f"{dataset} has no declared consumers"


def test_built_in_tables_exist_in_the_warehouse(seeded) -> None:
    """The lineage file names real tables, not ones somebody remembered."""
    declared = [
        asset_id for asset_id, spec in load_lineage().config.assets.items() if spec.kind == "table"
    ]

    assert declared
    for table in declared:
        assert seeded.table_exists(table), table


def test_downstream_follows_dependencies_transitively(tmp_path: Path) -> None:
    graph = load_lineage(_write(tmp_path, SMALL))

    reached = {asset.asset_id: asset for asset in graph.downstream("orders")}

    assert set(reached) == {"silver_orders", "orders_report", "pricing", "board_pack"}
    assert reached["pricing"].distance == 3
    assert reached["pricing"].via == ["orders", "silver_orders", "orders_report", "pricing"]
    assert "stock_dashboard" not in reached


def test_an_asset_reachable_two_ways_is_listed_once_at_its_shortest_path(tmp_path: Path) -> None:
    graph = load_lineage(_write(tmp_path, SMALL))

    packs = [asset for asset in graph.downstream("orders") if asset.asset_id == "board_pack"]

    assert len(packs) == 1
    assert packs[0].via == ["orders", "silver_orders", "board_pack"]


def test_built_in_lineage_separates_the_datasets() -> None:
    graph = load_lineage()

    from_sales = {asset.asset_id for asset in graph.downstream("pos_sales")}
    from_stock = {asset.asset_id for asset in graph.downstream("inventory")}

    # A decision two assets away from the dataset is still in its blast radius.
    assert "promo_budget_allocation" in from_sales
    assert "replenishment_orders" in from_sales & from_stock
    assert "demand_forecast" not in from_stock


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("sources: [a]\nassets:\n  x: {kind: report, depends_on: [nope]}", "nope"),
        (
            "sources: [a]\nassets:\n"
            "  x: {kind: report, depends_on: [a, y]}\n"
            "  y: {kind: report, depends_on: [x]}",
            "cycle",
        ),
        ("sources: [a]\nassets:\n  a: {kind: table, depends_on: [a]}", "both"),
        ("sources: [a]\nassets:\n  x: {kind: spreadsheet, depends_on: [a]}", "kind"),
        ("sources: [a]\nassets:\n  x: {kind: report, depends_on: [a], cost: 5}", "cost"),
        ("sources: [a]\ncost_model: {cost_per_record: {default: -1}}", "negative"),
        ("sources: [a]\ncost_model: {priority_thresholds: {p1: 1, p2: 2}}", "p2"),
        ("sources: [a\nassets", "YAML"),
        ("- just\n- a list", "mapping"),
    ],
)
def test_a_broken_lineage_file_is_rejected_with_a_reason(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(ImpactConfigError, match=message):
        load_lineage(_write(tmp_path, text))


def test_a_missing_lineage_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ImpactConfigError, match="cannot read"):
        load_lineage(tmp_path / "absent.yml")


# -- cost --------------------------------------------------------------------


def test_the_estimate_is_the_sum_of_its_three_terms(settings: Settings, tmp_path: Path) -> None:
    impact = assess_impact(_incident(), _settings_with(settings, _write(tmp_path, SMALL)))

    # report 10 + critical decision 100 x 3 + board pack override 500 = 810/h
    amounts = {line.label: line.amount for line in impact.cost.components}
    assert amounts == {
        "consumers running on bad data": 810 * 4,
        "notifying owners and re-checking": 3 * 25,
        "reprocessing records": 1000 * 0.1,
    }
    assert impact.cost.amount == 3420
    assert (impact.cost.low, impact.cost.high) == (2560, 4270)
    assert impact.cost.currency == "EUR"
    assert impact.priority == "P1"
    assert [asset.asset_id for asset in impact.consumers] == [
        "board_pack",
        "orders_report",
        "pricing",
    ]


def test_a_warning_is_charged_a_share_of_the_hourly_rate(
    settings: Settings, tmp_path: Path
) -> None:
    configured = _settings_with(settings, _write(tmp_path, SMALL))

    critical = assess_impact(_incident(), configured)
    warning = assess_impact(_incident(severity="warning"), configured)

    assert warning.cost.components[0].amount == critical.cost.components[0].amount / 2
    assert warning.priority == "P2"


def test_per_record_rate_can_differ_by_dataset(settings: Settings, tmp_path: Path) -> None:
    impact = assess_impact(
        _incident(dataset="stock"), _settings_with(settings, _write(tmp_path, SMALL))
    )

    assert impact.cost.components[-1].amount == 1000 * 1
    assert impact.count("dashboard") == 1


def test_missing_rows_count_as_records_at_risk() -> None:
    """A file that never arrived still puts a day of records at stake."""
    incident = _incident(
        evidence={"rows_loaded": 0},
        anomalies=[{"metric": "row_count", "observed": 0, "expected": 4200.4}],
    )

    assert records_at_risk(incident) == 4200


def test_the_estimate_says_it_is_an_estimate(settings: Settings, tmp_path: Path) -> None:
    impact = assess_impact(_incident(), _settings_with(settings, _write(tmp_path, SMALL)))

    assert "not a measured loss" in impact.cost.assumptions[0]
    assert any("4 hours" in assumption for assumption in impact.cost.assumptions)
    assert "estimated loss ~3,420 EUR" in headline(impact)


def test_a_dataset_outside_the_lineage_has_no_consumers(settings: Settings, tmp_path: Path) -> None:
    impact = assess_impact(
        _incident(dataset="returns"), _settings_with(settings, _write(tmp_path, SMALL))
    )

    assert impact.assets == []
    assert "not declared" in impact.cost.assumptions[0]
    assert impact.priority == "P3"


# -- the incident loop -------------------------------------------------------


def _drop(inject, next_day: date) -> None:
    inject(next_day, FaultSpec("pos_sales", FaultType.VOLUME_DROP, magnitude=0.65))


def test_every_new_incident_is_assessed_before_diagnosis(
    settings, seeded, next_day, inject
) -> None:
    _drop(inject, next_day)

    incident = monitor(next_day, seeded, settings, "test").incidents[0]

    assert incident.impact is not None
    assert incident.impact.count("decision") == 3
    assert incident.impact.cost.amount > 0
    assert incident.impact.cost.records_at_risk > incident.evidence["rows_loaded"]
    events = [event.event for event in incident.timeline]
    assert events.index("impact.assessed") < events.index("diagnosed")

    stored = IncidentStore(seeded).get(incident.incident_id)
    assert stored is not None
    assert stored.impact == incident.impact


def test_a_custom_lineage_file_replaces_the_built_in_one(
    settings, seeded, next_day, inject, tmp_path
) -> None:
    custom = SMALL.replace("sources: [orders, stock]", "sources: [orders, stock, pos_sales]")
    custom = custom.replace("depends_on: [orders]", "depends_on: [orders, pos_sales]")
    settings.impact.config_path = _write(tmp_path, custom)
    _drop(inject, next_day)

    incident = monitor(next_day, seeded, settings, "test").incidents[0]

    assert incident.impact is not None
    assert incident.impact.cost.currency == "EUR"
    assert {asset.asset_id for asset in incident.impact.consumers} == {
        "orders_report",
        "pricing",
        "board_pack",
    }


def test_a_broken_lineage_file_does_not_stop_the_repair(
    settings, seeded, next_day, inject, tmp_path
) -> None:
    settings.impact.config_path = _write(tmp_path, "sources: []")
    _drop(inject, next_day)

    incident = monitor(next_day, seeded, settings, "test").incidents[0]

    assert incident.impact is None
    assert incident.diagnosis is not None
    assert incident.actions
    assert "impact.unavailable" in [event.event for event in incident.timeline]


def test_impact_assessment_can_be_switched_off(settings, seeded, next_day, inject) -> None:
    settings.impact.enabled = False
    _drop(inject, next_day)

    incident = monitor(next_day, seeded, settings, "test").incidents[0]

    assert incident.impact is None
    assert "impact.unavailable" not in [event.event for event in incident.timeline]


def test_incidents_recorded_before_impact_existed_still_load() -> None:
    payload = _incident().model_dump(mode="json")
    del payload["impact"]

    assert Incident.model_validate(payload).impact is None


# -- the agent's context -----------------------------------------------------


def test_the_agent_brief_carries_the_blast_radius_and_the_estimate(settings: Settings) -> None:
    incident = _incident(dataset="pos_sales")
    incident.impact = assess_impact(incident, settings)

    brief = incident_brief(incident)

    assert "Business impact" in brief
    assert "the money figure is an estimate" in brief
    assert f"Priority: {incident.impact.priority}" in brief
    assert "decision: Automatic replenishment orders [critical, owner Supply planning]" in brief
    assert "via silver_pos_sales > demand_forecast" in brief
    assert "silver_pos_sales [" not in brief, "tables are plumbing, not consumers"
    assert brief.endswith("Investigate, then call submit_diagnosis.")


def test_a_brief_without_an_assessment_says_nothing_about_impact() -> None:
    assert "Business impact" not in incident_brief(_incident())


def test_the_reviewer_sees_the_same_impact(settings: Settings) -> None:
    incident = _incident(dataset="pos_sales")
    incident.impact = assess_impact(incident, settings)

    brief = review_brief(incident, [ActionUnderReview(name="enable_dedupe", arguments={})])

    assert "Business impact" in brief
    assert "submit_diagnosis" not in brief
    assert brief.endswith("then call submit_review.")


# -- API and console ---------------------------------------------------------


@pytest.fixture
def client(settings, monkeypatch) -> TestClient:
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.delenv(APPROVER_TOKENS_ENV, raising=False)
    return TestClient(api.app, client=("127.0.0.1", 50000))


@pytest.fixture
def held_incident(settings, seeded, next_day, inject) -> Incident:
    """An incident that stays open: contained with a hold, not fixed."""
    _drop(inject, next_day)
    incident = monitor(next_day, seeded, settings, "test").incidents[0]
    seeded.close()
    return incident


def test_the_incident_list_shows_reach_and_estimate(client, held_incident) -> None:
    listed = client.get("/api/incidents").json()[0]["impact"]

    assert listed["priority"] == held_incident.impact.priority
    assert listed["consumers"] == 7
    assert listed["decisions"] == 3
    assert listed["estimated_loss"] == held_incident.impact.cost.amount
    assert listed["is_estimate"] is True


def test_the_incident_document_carries_the_full_assessment(client, held_incident) -> None:
    document = client.get(f"/api/incidents/{held_incident.incident_id}").json()

    impact = document["impact"]
    assert {asset["kind"] for asset in impact["assets"]} >= {"table", "decision", "dashboard"}
    assert len(impact["cost"]["components"]) == 3
    assert impact["cost"]["assumptions"]


def test_the_summary_totals_what_open_incidents_put_at_stake(client, held_incident) -> None:
    at_stake = client.get("/api/summary").json()["open_estimated_loss"]

    assert at_stake["amount"] == held_incident.impact.cost.amount
    assert at_stake["incidents"] == 1
    assert at_stake["is_estimate"] is True


def test_the_lineage_endpoint_answers_for_one_dataset(client) -> None:
    body = client.get("/api/lineage", params={"dataset": "inventory"}).json()

    assert body["sources"] == ["inventory"]
    names = {asset["asset_id"] for asset in body["downstream"]["inventory"]}
    assert "inventory_coverage_dashboard" in names
    assert body["cost_model"]["exposure_hours"] == 8
    assert client.get("/api/lineage", params={"dataset": "nope"}).status_code == 404


def test_the_lineage_endpoint_reports_a_broken_file(client, settings, tmp_path) -> None:
    settings.impact.config_path = _write(tmp_path, "sources: []")

    assert client.get("/api/lineage").status_code == 503


def test_the_console_renders_the_impact_panel(client) -> None:
    page = client.get("/").text

    assert "Blast radius and estimated loss" in page
    assert "estimate, not a measurement" in page


# -- CLI ---------------------------------------------------------------------


@pytest.fixture
def cli_env(settings, monkeypatch):
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("SENTINEL_AGENT__PROVIDER", "rules")
    monkeypatch.setenv("COLUMNS", "200")
    reset_settings_cache()
    yield monkeypatch
    reset_settings_cache()


def test_show_is_the_incident_report_with_consumers_and_arithmetic(cli_env, held_incident) -> None:
    result = CliRunner().invoke(app, ["show", held_incident.incident_id])

    assert result.exit_code == 0, result.output
    assert "Automatic replenishment orders" in result.output
    assert "not a measurement" in result.output
    assert "reprocessing records" in result.output


def test_impact_command_lists_what_is_downstream(cli_env) -> None:
    result = CliRunner().invoke(app, ["impact", "shipments"])

    assert result.exit_code == 0, result.output
    assert "Customer service level (OTIF) report" in result.output
    assert "SKU demand forecast" not in result.output


def test_impact_check_fails_on_a_broken_file(cli_env, tmp_path) -> None:
    cli_env.setenv("SENTINEL_IMPACT__CONFIG_PATH", str(_write(tmp_path, "sources: []")))
    reset_settings_cache()

    result = CliRunner().invoke(app, ["impact", "--check"])

    assert result.exit_code == 1
    assert "invalid" in result.output


def test_impact_check_passes_on_the_built_in_file(cli_env) -> None:
    result = CliRunner().invoke(app, ["impact", "--check"])

    assert result.exit_code == 0, result.output
    assert "3 source(s)" in result.output
