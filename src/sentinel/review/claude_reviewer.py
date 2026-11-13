"""The Claude reviewer.

The same manual tool-use loop as the diagnosis engine, with one difference in how
it fails. When the diagnosis engine cannot reach Claude it falls back to rules,
because a platform that stops diagnosing is worse than one that diagnoses simply.
A reviewer that cannot review does the opposite of falling back: it escalates
everything. No verdict is not an approval.
"""

from __future__ import annotations

import json
from typing import Any, cast

import anthropic
from anthropic.types import MessageParam, OutputConfigParam, ToolParam

from sentinel.agent.tools import TOOL_SCHEMAS, Toolbox
from sentinel.config import ReviewerSettings
from sentinel.incidents.models import ActionReview, ReviewVerdict
from sentinel.logging import get_logger
from sentinel.review.base import ReviewRequest, escalate_all
from sentinel.review.prompts import REVIEW_SYSTEM_PROMPT, SUBMIT_REVIEW_TOOL, review_brief

log = get_logger(__name__)

#: Tool results are truncated before being sent back, as in the diagnosis engine.
MAX_TOOL_RESULT_CHARS = 8000


class ClaudeReviewer:
    """Reviews proposed actions with Claude; escalates everything on any failure."""

    def __init__(
        self, settings: ReviewerSettings, client: anthropic.Anthropic | None = None
    ) -> None:
        self.settings = settings
        self.name = f"claude:{settings.model}"
        self._client = client or anthropic.Anthropic(api_key=settings.api_key)

    def review(self, request: ReviewRequest) -> list[ActionReview]:
        """Run the tool-use loop and return one review per action."""
        incident_id = request.incident.incident_id
        try:
            payload = self._run_loop(request)
        except anthropic.APIError as error:
            log.warning(
                "reviewer.unavailable",
                incident_id=incident_id,
                error=f"{type(error).__name__}: {error}",
            )
            return escalate_all(
                request,
                self.name,
                f"reviewer unavailable (Claude API error: {type(error).__name__})",
            )

        if payload is None:
            return escalate_all(request, self.name, "the reviewer did not submit a verdict")
        return self._parse(payload, request)

    # -- internals ---------------------------------------------------------

    def _run_loop(self, request: ReviewRequest) -> dict[str, Any] | None:
        incident = request.incident
        toolbox = Toolbox(request.warehouse, incident.business_date, incident=incident)
        tools: list[dict[str, Any]] = [*TOOL_SCHEMAS, SUBMIT_REVIEW_TOOL]
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": review_brief(incident, request.actions)}
        ]

        for _ in range(self.settings.max_tool_calls):
            response = self._client.messages.create(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                system=REVIEW_SYSTEM_PROMPT,
                tools=cast("list[ToolParam]", tools),
                messages=cast("list[MessageParam]", messages),
                output_config=cast("OutputConfigParam", {"effort": self.settings.effort}),
            )

            if response.stop_reason == "refusal":
                log.warning("reviewer.refused", incident_id=incident.incident_id)
                return None

            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if not tool_uses:
                return None

            submissions = [block for block in tool_uses if block.name == "submit_review"]
            if submissions:
                return dict(submissions[0].input)

            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": _encode(toolbox.call(block.name, dict(block.input))),
                }
                for block in tool_uses
            ]
            messages.append({"role": "user", "content": results})

        log.warning(
            "reviewer.budget_exhausted",
            incident_id=incident.incident_id,
            max_tool_calls=self.settings.max_tool_calls,
        )
        return None

    def _parse(self, payload: dict[str, Any], request: ReviewRequest) -> list[ActionReview]:
        """Turn ``submit_review`` arguments into one review per action.

        Anything malformed counts against approval: an action the reviewer did not
        mention, mentioned twice with different verdicts, or gave an unknown
        verdict for is escalated.
        """
        reviews = escalate_all(
            request, self.name, "the reviewer returned no verdict for this action"
        )
        seen: set[int] = set()

        for item in payload.get("reviews") or []:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(reviews):
                continue
            if index in seen:
                reviews[index] = ActionReview(
                    verdict=ReviewVerdict.ESCALATE,
                    reason="the reviewer gave more than one verdict for this action",
                    reviewer=self.name,
                )
                continue
            seen.add(index)

            approved = str(item.get("verdict")) == ReviewVerdict.APPROVE
            reviews[index] = ActionReview(
                verdict=ReviewVerdict.APPROVE if approved else ReviewVerdict.ESCALATE,
                reason=str(item.get("reason") or "")[:600],
                reviewer=self.name,
            )
        return reviews


def _encode(result: dict[str, Any]) -> str:
    """Serialise a tool result, truncating pathologically large payloads."""
    text = json.dumps(result, default=str)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + '... [truncated]"}'
