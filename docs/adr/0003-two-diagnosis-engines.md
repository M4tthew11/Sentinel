# 3. Two diagnosis engines, one interface

*Status: accepted*

## Context

The interesting part of this platform is the step between "something is wrong"
and "here is what to do about it". An LLM is a good fit: the evidence is
heterogeneous (failed checks, statistical signals, schema listings, sample rows),
the reasoning is the kind of pattern-matching engineers do out loud, and the
output is a short structured verdict.

It is also a bad fit for three specific situations: the reviewer who cloned the
repository has no API key, CI must be deterministic and free, and production must
keep working when a provider has an outage.

## Decision

Define one protocol, `DiagnosisEngine.diagnose(request) -> Diagnosis`, and ship
two implementations:

- **`RuleBasedEngine`** - an explicit decision tree over the same evidence. It is
  not a stub: it runs the demo, runs in CI, and is the baseline the LLM is scored
  against.
- **`ClaudeEngine`** - a tool-use loop with five read-only investigation tools and
  a terminal `submit_diagnosis` tool that carries the structured verdict.

Selection is by configuration with a sensible default (`auto`): use Claude when
`ANTHROPIC_API_KEY` is present, otherwise the rule engine. Any failure inside the
LLM path - API error, refusal, or a loop that never submits - degrades to the
rule engine and records why on the incident.

## Consequences

**Good.** The repository runs offline. CI is deterministic. The eval harness makes
"is the LLM worth it here?" an empirical question with a real baseline rather than
an assumption - and on a one-fault-per-day scenario the decision tree scores 100%,
which is a useful and slightly uncomfortable result to be able to state.

**Bad.** Two implementations of the same behaviour drift unless both are exercised.
Mitigated by running both through the same eval harness and by the rule engine
being on the fallback path, so it cannot rot unnoticed.

**Where the LLM should win.** Faults outside the injected catalogue, several faults
interacting on the same day, and evidence that needs a tool call the decision tree
does not know to make. That is the honest scope of the comparison: the rule engine
is better at the seven failure modes someone already enumerated, and worse at the
eighth.
