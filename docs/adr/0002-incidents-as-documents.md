# 2. Incidents as documents, in the same warehouse

*Status: accepted*

## Context

An incident accumulates structure as it moves: the checks that failed, the
anomaly signals, an evidence bundle, a diagnosis with free-text reasoning, a list
of proposed actions each with its own status and result, and a timeline of who
did what. The shape changes as the platform grows - the diagnosis gained
`tool_calls` and `latency_ms` during development, and the evidence bundle changed
three times.

Modelling that as five normalised tables would mean a migration for every change
and a five-way join to read one incident. Modelling it as one wide table would
mean a column per possible field and a lot of nulls.

## Decision

Store incidents as JSON documents with promoted columns for filtering:

```sql
ops_incident(incident_id, run_id, business_date, dataset, severity, status,
             detected_at, updated_at, document JSON)
```

The document is a Pydantic model (`sentinel.incidents.models.Incident`) that
validates on the way in and on the way out. The promoted columns are exactly the
ones the UI and the API filter on; everything else lives in the document.

## Consequences

**Good.** Schema evolution is a Pydantic default away. The API can return the
whole incident with no assembly. Validation is enforced in one place, and a
malformed document fails loudly at read time rather than producing a half-empty
object.

**Bad.** Ad-hoc SQL over incident internals means JSON extraction. Acceptable:
the analytical queries in this platform are about *data*, not about incidents.

**Note on the target environment.** This is a document store with a relational
index in front of it - the same shape as Cosmos DB or MongoDB, which is where it
would live in a production deployment. Keeping it in DuckDB removes a container
from the demo without changing the access pattern: `IncidentStore` exposes
`save` / `get` / `search`, and swapping the backend means reimplementing three
methods.
