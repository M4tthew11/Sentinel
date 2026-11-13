# 7. Declared lineage and a loss estimate that says it is one

*Status: accepted*

## Context

An incident said what broke and what was done about it. It did not say who was
looking at the broken data, or how much that mattered. Those are the first two
questions a person asks, and without them every incident has the same weight: a
null spike in a table nobody reads ranks alongside a truncated sales file that
feeds automatic ordering.

Two things were missing: the set of reports, dashboards and decisions downstream
of a dataset, and some way to compare incidents in a common unit.

## Decision

**Lineage is declared in a YAML file, not discovered.** `impact.yml` lists the
assets built from each source dataset and what each one reads. Sentinel follows
the dependencies transitively, so a decision that reads a dashboard that reads a
gold table is in the blast radius of the dataset underneath. Parsing the
pipeline's SQL would find the tables and stop there; it cannot know that a planner
opens a dashboard every morning. The consumers that decide what an incident costs
live outside the warehouse, and only the people running the platform can name
them. The file is validated on load: unknown references, cycles and misspelt keys
are errors that name what to fix.

**The loss is an estimate built from configured rates, and is labelled as one
everywhere.** Three terms, each a rate from the file times a count Sentinel does
know: an hourly rate per affected consumer times an assumed exposure window, a
flat cost per consumer, and a cost per record at risk. The incident stores the
arithmetic and the assumptions next to the total, the total is reported with a
range, and the console, the CLI report and the agent's brief all call it an
estimate. Its use is ranking incidents against each other.

**Assessment happens when the incident opens, before diagnosis.** It depends only
on the dataset, the severity and the partition size, so it is available to the
engine as context and to a human before anything else has run.

**Impact informs urgency, never cause or permission.** The agent and the reviewer
both see it. The prompts say what it is for: contain first when decisions are
downstream, and treat high stakes as a reason to be surer before changing data.
The hard rules and the risk gate do not read it. A large number cannot make an
action run that would otherwise wait for a human.

**A broken lineage file costs an incident its impact section and nothing else.**
The loop still diagnoses and repairs; the timeline records why impact is missing.
`sentinel impact --check` validates the file before it is deployed.

## Consequences

- The estimate is exactly as good as the rates somebody typed. With the shipped
  defaults it is illustrative.
- The exposure window is assumed, not measured. The estimate is what an incident
  puts at stake, not what it ended up costing after Sentinel contained it.
- Lineage is per dataset, not per column: a fault in one measure counts every
  consumer of the dataset, including those that never read that measure.
- The file can drift from reality. A test checks that the tables it names exist;
  nothing can check that a dashboard still does.
