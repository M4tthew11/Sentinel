# 4. Remediation as a whitelist with a risk gate

*Status: accepted*

## Context

Self-healing means a model's output causes a change to production data. The
obvious implementations are all unsafe: letting the agent emit SQL, letting it
call arbitrary tools, or letting it write to the warehouse directly. Data in
these pipelines is also adversarial by nature - it comes from third parties, and
a row value that reads like an instruction must never become one.

## Decision

**Reading and writing are separate surfaces.** The agent's tools are read-only.
It cannot change anything by calling a tool; it can only *propose* actions as
structured names and arguments in its final diagnosis.

**Actions come from a registry, not from the model.** `sentinel.remediation.actions`
defines every permitted action with a JSON-schema parameter spec, a risk level and
a handler. A proposal whose name is not in the registry is recorded as failed and
never executed. Arguments are validated - unknown or missing ones are rejected
before the handler runs.

**Actions are configuration changes, not data mutations.** They write to
operational tables: a column mapping, a feature flag, a quarantine marker, a
publication hold, a ticket. Because silver is a full rebuild from bronze, a fix is
applied by rebuilding, and undone by deleting a row and rebuilding again. Nothing
issues destructive DML.

**Risk decides autonomy.** `RiskLevel.HIGH` actions never auto-execute, whatever
the configured mode. Today that is `apply_unit_correction`, the one action that
rewrites reported business figures. In `manual` mode nothing auto-executes at all.

**The executor verifies its own work.** After applying changes it re-runs the
pipeline and re-checks the data, and only closes an incident as `resolved` when
the checks pass. Containment without a cure is recorded as `remediated`, which is
a different and less flattering state.

## Consequences

**Good.** The blast radius is enumerable: eight actions, each auditable and
reversible. A prompt injection in a row value cannot escalate beyond making the
agent read something else. The risk gate is enforced in the executor, not in the
prompt, so it holds even if the model ignores its instructions.

**Bad.** The agent cannot fix anything nobody anticipated - a genuinely novel
failure ends as a ticket. That is the correct trade for autonomy over production
data, and the ticket is a better outcome than a creative repair.

**Consequence for prompt design.** The whitelist is rendered into the system
prompt from the registry, so the model's option set and the executor's permission
set cannot drift apart.
