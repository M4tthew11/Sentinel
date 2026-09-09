# 1. DuckDB as the warehouse

*Status: accepted*

## Context

The platform needs a real analytical warehouse: multi-table joins, window
functions, aggregate scans over a few million rows, and SQL that a data engineer
would recognise. It also needs to be runnable by someone who has just cloned the
repository, on a laptop, with no account, no cluster and no credentials.

Those two requirements pull in opposite directions. Databricks or Synapse would
match the target environment but make the project unrunnable for a reviewer.
SQLite or pandas would run anywhere but would not be a warehouse in any
meaningful sense - and modelling a medallion architecture on a row store teaches
the wrong lessons about what is cheap and what is expensive.

## Decision

Use DuckDB as an embedded analytical engine, and keep the SQL portable.

Concretely:

- Transformations are plain SQL strings, not an ORM or a dataframe pipeline.
- Bronze stores the source payload as JSON with load metadata; silver is a typed
  projection; gold is aggregated. This is the same layering a lakehouse uses.
- Nothing depends on DuckDB-specific syntax beyond `json_extract_string` and
  `QUALIFY`, both of which have direct equivalents in Databricks SQL.
- The warehouse is reached only through `sentinel.elt.warehouse.Warehouse`, so
  the connection, the DDL and the query helpers are a single seam to replace.

## Consequences

**Good.** `sentinel demo` builds a 50-day warehouse in about two minutes with no
setup. Tests run against the real engine rather than a mock, so a SQL mistake
fails in CI. Reviewers can open `data/warehouse.duckdb` in any DuckDB client and
poke at it themselves.

**Bad.** Single-process writes: the API opens its own connection per request and
the pipeline holds the write lock while it runs. That is fine for one node and
wrong for a cluster - a real deployment would move silver and gold onto
Databricks and keep only the operational tables here.

**Accepted risk.** Some of the performance intuitions this project builds (a full
silver rebuild every run is cheap) hold for an embedded engine on 100k rows and
would need revisiting at 100 million.
