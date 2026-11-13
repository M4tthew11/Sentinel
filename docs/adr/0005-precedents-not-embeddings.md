# 5. Precedents by signal overlap, not embeddings

*Status: accepted*

## Context

The platform closes incidents and then forgets them. Each one records the signals
that opened it, the diagnosis, every action and its result, and whether
verification or a human agreed. That is exactly what an on-call engineer asks for
first: has this happened before, and what did we do?

The usual way to give an agent a memory is to embed past cases and retrieve by
vector similarity. Here that would add a model, an index and a network call to
answer a question the data already answers structurally.

There is also a risk in the feature itself. History is written by the same engine
that reads it. A wrong diagnosis that is retrieved, trusted and repeated becomes
more entrenched every time.

## Decision

**Similarity is Jaccard overlap of an incident's signature.** The signature is
the set of failed checks and anomalous metrics, with direction, reduced to their
family (`measure_avg__units_sold` becomes `measure_avg`) so precedents carry
across datasets. A precedent from another dataset is weighted down. Every match
returns the signals it shares.

**Only incidents with an outcome are precedents.** Resolved, remediated,
dismissed, or holding an action a human rejected. Open and awaiting-approval
incidents are excluded: nothing has tested them yet. Lookup is limited to
incidents detected strictly before the one being diagnosed.

**It is one more read-only tool.** `find_similar_incidents` sits next to the
other investigation tools in `Toolbox`, so the read/write split of ADR 4 is
untouched. It returns the outcome next to the diagnosis: which actions ran, which
failed, which a human rejected and why, and what verification said.

**The prompt treats a precedent as a lead.** The agent is told to confirm it on
today's data, to weigh how the incident ended, not to re-propose a rejected
action unless today's evidence answers the rejection, and to cite the incident
it relied on. Text from the incident record is information, never instruction.

**The rule engine does not use history.** It stays the fixed baseline (ADR 3).

## Consequences

**Good.** No new dependency, no index to keep in sync, deterministic results that
a test can pin down. Human approvals and rejections reach the agent without any
retraining. `Diagnosis.tools_used` records whether history was consulted, and the
`recurring` eval scenario measures what the engine does with it, including a
lookalike day where copying the precedent is the wrong answer.

**Bad.** Signal overlap is coarse. Two different faults can share a signature,
which is why the lookalike day exists. It also cannot match on free text, such as
a ticket describing an upstream release. If the fault catalogue grows past what
checks and metrics distinguish, an embedding over the diagnosis text becomes the
next step, behind the same tool.

**Bad.** A scan of the newest 500 incident documents per lookup is fine on DuckDB
at this size. On a real document store the signature would be a promoted, indexed
field.

**Open.** Whether history improves the Claude engine is not yet measured. The
harness can measure it; the run needs an API key.
