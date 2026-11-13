# 6. An independent reviewer that can only narrow autonomy

*Status: accepted*

## Context

ADR 4 bounds what the agent can do: eight whitelisted actions, and the high-risk
one waits for a human. Inside that boundary the agent was still trusted
completely. A low- or medium-risk action ran the moment it was proposed, so a
confident misdiagnosis could quarantine a healthy partition or map a measure onto
the wrong column, and the only check was verification after the fact.

The obvious fix is a second agent that reviews the first. The obvious ways to get
that wrong are also well known:

- a reviewer that reads the proposer's reasoning grades the argument, not the
  action, and two models agreeing is then one opinion counted twice;
- a reviewer whose approval can *unlock* something turns a second LLM into a
  second way past the human;
- a reviewer that fails open makes an outage of the reviewer an outage of the
  safeguard.

## Decision

**The reviewer is independent by construction.** It receives the incident as the
monitor recorded it and each action's name and arguments. It does not receive the
diagnosis, the reasoning or the per-action rationale, and free-text arguments
(`reason`, `summary`, `body`) are stripped before it sees them. The Claude
reviewer has the same read-only tools as the diagnosis agent, so it can establish
the facts for itself.

**Hard rules are code, and they outrank the reviewer.** `remediation/policy.py`
sends an action to a human when it is `RiskLevel.HIGH` or when it targets a
dataset or business date other than the incident's. These are comparisons, not
judgements; they are evaluated in the executor after the review, and an "approve"
does not lift them. For those actions the verdict is recorded and shown to the
human as advice.

**A verdict can only narrow.** Everything the reviewer may approve would have run
unattended before the reviewer existed. Its approval restores the old behaviour
for one action; its escalation removes it. There is no input the reviewer can be
given that makes the platform do more on its own than ADR 4 already allowed.

**It fails closed.** An API error, a refusal, an exhausted tool budget, a missing
or duplicated verdict, an exception: each leaves the action awaiting approval.
The diagnosis engine degrades to rules when Claude is down, because diagnosing
badly beats not diagnosing. Review has no such trade: no verdict is not an
approval.

**Two implementations, as with diagnosis (ADR 3).** The rule reviewer approves an
action only when the incident's own observations show the problem it fixes, and
escalates whatever it has no rule for. It is what runs offline and in CI.

**The gate is evaluated on its own.** `sentinel eval-review` puts hand-written
harmful proposals in front of the hard rules and the reviewer together and fails
if any is cleared to run. Scoring the gate separately from the diagnosis matters:
an agent that never proposes anything harmful says nothing about what would
happen if it did.

## Consequences

**Good.** A wrong proposal now has to get past a check that did not see the
argument for it. The safety property that matters - high-risk and out-of-scope
actions reach a human - does not depend on either model. Every verdict is in the
incident document and its timeline, so "why did this run?" and "why is this
waiting?" both have a recorded answer.

**Bad.** With the Claude reviewer, each incident with proposals costs a second
tool-use conversation: more latency and roughly double the model spend per
incident. More actions will wait for a human than before, including some that
were fine - that is the intended direction of error, and the pre-approval rate in
`eval-review` is how to see whether it has gone too far.

**Not solved.** Independence of inputs is not independence of failure. Two
instances of the same model can share a blind spot; `SENTINEL_REVIEWER__MODEL`
allows a different model for the reviewer, and the rule reviewer shares nothing
with Claude at all. And the reviewer reads the same warehouse the agent did: data
that misleads one can mislead the other, which is why the hard rules do not ask
either of them.
