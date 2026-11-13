"""Reviewer interface and selection.

The diagnosis agent proposes actions; a reviewer checks them before the executor
lets any of them run without a human. The point of a second opinion is that it is
*independent*, so a reviewer is given the incident as the monitor recorded it and
the bare action - its name and arguments - and nothing the proposing agent wrote
about why: no diagnosis, no reasoning, no rationale. It has to find the
justification in the data itself, or fail to.

Like the diagnosis engines, there are two reviewers behind one protocol:

* :class:`~sentinel.review.rules.RuleReviewer` - deterministic, offline, free;
* :class:`~sentinel.review.claude_reviewer.ClaudeReviewer` - Claude with the
  same read-only tools the diagnosis agent has.

A reviewer can only *narrow* autonomy. Its "approve" lets a low-risk action run
that would have run anyway before reviewers existed; its "escalate" sends that
action to a human instead. What must always reach a human is decided by
:mod:`sentinel.remediation.policy`, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sentinel.config import ReviewerProvider, Settings, get_settings
from sentinel.elt.warehouse import Warehouse
from sentinel.incidents.models import ActionReview, Incident, ReviewVerdict
from sentinel.logging import get_logger

log = get_logger(__name__)

#: Arguments that carry prose written by the proposing agent. They are withheld
#: from the reviewer: a ticket body is not evidence, and it is the one place the
#: first agent could argue its case to the second.
FREE_TEXT_ARGUMENTS: frozenset[str] = frozenset({"reason", "summary", "body"})


@dataclass(frozen=True)
class ActionUnderReview:
    """One proposed action as the reviewer sees it."""

    name: str
    arguments: dict[str, Any]

    @classmethod
    def from_proposal(cls, name: str, arguments: dict[str, Any]) -> ActionUnderReview:
        """Strip the proposing agent's prose from the arguments."""
        return cls(
            name=name,
            arguments={
                key: value for key, value in arguments.items() if key not in FREE_TEXT_ARGUMENTS
            },
        )


@dataclass
class ReviewRequest:
    """Everything a reviewer needs to judge the actions proposed for one incident."""

    incident: Incident
    actions: list[ActionUnderReview]
    warehouse: Warehouse
    settings: Settings


@runtime_checkable
class Reviewer(Protocol):
    """Judges proposed actions against the evidence."""

    name: str

    def review(self, request: ReviewRequest) -> list[ActionReview]:
        """Return one review per action, in the order the actions were given."""
        ...


def escalate_all(request: ReviewRequest, reviewer: str, reason: str) -> list[ActionReview]:
    """The answer when a reviewer cannot do its job: every action goes to a human."""
    return [
        ActionReview(verdict=ReviewVerdict.ESCALATE, reason=reason, reviewer=reviewer)
        for _ in request.actions
    ]


def build_reviewer(settings: Settings | None = None) -> Reviewer | None:
    """Pick a reviewer according to configuration; ``None`` when reviewing is off."""
    settings = settings or get_settings()
    provider = settings.reviewer.provider
    has_key = bool(settings.reviewer.api_key)

    if provider is ReviewerProvider.OFF:
        return None

    if provider is ReviewerProvider.CLAUDE and not has_key:
        raise RuntimeError(
            "SENTINEL_REVIEWER__PROVIDER=claude but ANTHROPIC_API_KEY is not set. "
            "Set the key, or use provider=auto to fall back to the rule reviewer."
        )

    if provider is ReviewerProvider.CLAUDE or (provider is ReviewerProvider.AUTO and has_key):
        from sentinel.review.claude_reviewer import ClaudeReviewer

        return ClaudeReviewer(settings.reviewer)

    from sentinel.review.rules import RuleReviewer

    return RuleReviewer()
