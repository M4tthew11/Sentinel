"""Persistence for incident documents.

Incidents live as JSON documents with a handful of promoted columns for
filtering. That is a document store with a relational index in front of it -
the same shape you would deploy on Cosmos DB or MongoDB, kept here on DuckDB so
the demo has no external dependency (see docs/adr/0002-incident-store.md).
"""

from __future__ import annotations

from datetime import date

from sentinel.elt.warehouse import Warehouse
from sentinel.incidents.models import Incident, IncidentStatus, utcnow
from sentinel.logging import get_logger

log = get_logger(__name__)


class IncidentStore:
    """Read/write access to incident documents."""

    def __init__(self, warehouse: Warehouse) -> None:
        self.warehouse = warehouse

    def save(self, incident: Incident) -> Incident:
        """Insert or replace an incident document."""
        incident.updated_at = utcnow()
        self.warehouse.execute(
            """
            INSERT OR REPLACE INTO ops_incident
                (incident_id, run_id, business_date, dataset, severity, status,
                 detected_at, updated_at, document)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                incident.incident_id,
                incident.run_id,
                incident.business_date,
                incident.dataset,
                incident.severity,
                str(incident.status),
                incident.detected_at,
                incident.updated_at,
                incident.to_json(),
            ],
        )
        log.debug(
            "incident.saved",
            incident_id=incident.incident_id,
            status=str(incident.status),
            dataset=incident.dataset,
        )
        return incident

    def get(self, incident_id: str) -> Incident | None:
        """Fetch one incident by id."""
        row = self.warehouse.fetch_one(
            "SELECT document FROM ops_incident WHERE incident_id = ?", [incident_id]
        )
        return None if row is None else Incident.from_json(str(row[0]))

    def search(
        self,
        business_date: date | None = None,
        dataset: str | None = None,
        status: IncidentStatus | None = None,
        limit: int = 100,
    ) -> list[Incident]:
        """List incidents, newest first, with optional filters."""
        clauses: list[str] = []
        params: list[object] = []
        if business_date is not None:
            clauses.append("business_date = ?")
            params.append(business_date)
        if dataset is not None:
            clauses.append("dataset = ?")
            params.append(dataset)
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        frame = self.warehouse.fetch_df(
            f"""
            SELECT document
            FROM ops_incident
            {where}
            ORDER BY detected_at DESC, incident_id DESC
            LIMIT ?
            """,
            params,
        )
        return [Incident.from_json(str(value)) for value in frame["document"].tolist()]

    def open_incidents(self) -> list[Incident]:
        """Every incident that is neither resolved nor dismissed."""
        frame = self.warehouse.fetch_df(
            """
            SELECT document
            FROM ops_incident
            WHERE status NOT IN ('resolved', 'dismissed')
            ORDER BY detected_at DESC
            """
        )
        return [Incident.from_json(str(value)) for value in frame["document"].tolist()]

    def count(self) -> int:
        """Total number of incidents on record."""
        return int(self.warehouse.scalar("SELECT count(*) FROM ops_incident") or 0)
