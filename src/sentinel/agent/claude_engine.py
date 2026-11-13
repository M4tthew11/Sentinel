"""The Claude tool-use engine.

A manual agentic loop rather than the SDK tool runner, for two reasons: the loop
must stop the moment ``submit_diagnosis`` is called (the diagnosis is the exit
condition, not "the model ran out of tools to call"), and every failure path -
API error, refusal, budget exhaustion - has to degrade to the rule engine rather
than raise. A data platform that stops self-healing because an LLM provider is
having a bad afternoon is worse than one that never used an LLM.

The model can only *read* through tools; it proposes remediation as structured
arguments, which the executor validates against a whitelist before anything runs
(see :mod:`sentinel.remediation.actions`).
"""

from __future__ import annotations

import json
import time
from typing import Any, cast

import anthropic
from anthropic.types import MessageParam, OutputConfigParam, ToolParam

from sentinel.agent.base import DiagnosisRequest
from sentinel.agent.prompts import SUBMIT_TOOL, SYSTEM_PROMPT, incident_brief
from sentinel.agent.rules import RuleBasedEngine
from sentinel.agent.tools import TOOL_SCHEMAS, Toolbox
from sentinel.config import AgentSettings
from sentinel.incidents.models import Diagnosis, ProposedAction, RootCause
from sentinel.logging import get_logger

log = get_logger(__name__)

#: Tool results are truncated before being sent back, so one wide query cannot
#: blow up the context window (and the bill) on a pathological partition.
MAX_TOOL_RESULT_CHARS = 8000


class ClaudeEngine:
    """Diagnoses incidents with Claude, falling back to rules on any failure."""

    name = "claude"

    def __init__(self, settings: AgentSettings, client: anthropic.Anthropic | None = None) -> None:
        self.settings = settings
        self._client = client or anthropic.Anthropic(api_key=settings.api_key)
        self._fallback = RuleBasedEngine()

    def diagnose(self, request: DiagnosisRequest) -> Diagnosis:
        """Run the tool-use loop and return the model's structured conclusion."""
        started = time.perf_counter()
        try:
            diagnosis = self._run_loop(request)
        except anthropic.APIError as error:
            log.warning(
                "claude.unavailable",
                incident_id=request.incident.incident_id,
                error=f"{type(error).__name__}: {error}",
            )
            return self._degrade(request, f"Claude API error: {type(error).__name__}")

        if diagnosis is None:
            return self._degrade(request, "the model did not submit a diagnosis")

        diagnosis.latency_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "claude.diagnosed",
            incident_id=request.incident.incident_id,
            root_cause=str(diagnosis.root_cause),
            confidence=diagnosis.confidence,
            tool_calls=diagnosis.tool_calls,
            latency_ms=diagnosis.latency_ms,
        )
        return diagnosis

    # -- internals ---------------------------------------------------------

    def _run_loop(self, request: DiagnosisRequest) -> Diagnosis | None:
        incident = request.incident
        toolbox = Toolbox(request.warehouse, incident.business_date, incident=incident)
        tools: list[dict[str, Any]] = [*TOOL_SCHEMAS, SUBMIT_TOOL]
        messages: list[dict[str, Any]] = [{"role": "user", "content": incident_brief(incident)}]

        for _ in range(self.settings.max_tool_calls):
            response = self._client.messages.create(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                system=SYSTEM_PROMPT,
                tools=cast("list[ToolParam]", tools),
                messages=cast("list[MessageParam]", messages),
                output_config=cast("OutputConfigParam", {"effort": self.settings.effort}),
            )

            if response.stop_reason == "refusal":
                log.warning("claude.refused", incident_id=incident.incident_id)
                return None

            # Append the assistant turn verbatim: thinking blocks must be
            # replayed unchanged for the next request to be accepted.
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if not tool_uses:
                return None

            submissions = [block for block in tool_uses if block.name == "submit_diagnosis"]
            if submissions:
                return self._parse_diagnosis(submissions[0].input, toolbox.call_log)

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
            "claude.budget_exhausted",
            incident_id=incident.incident_id,
            max_tool_calls=self.settings.max_tool_calls,
        )
        return None

    def _parse_diagnosis(self, payload: dict[str, Any], tools_used: list[str]) -> Diagnosis:
        """Convert the model's ``submit_diagnosis`` arguments into a Diagnosis."""
        try:
            root_cause = RootCause(str(payload.get("root_cause", "unknown")))
        except ValueError:
            root_cause = RootCause.UNKNOWN

        contributing: list[RootCause] = []
        for raw in payload.get("contributing_causes") or []:
            try:
                cause = RootCause(str(raw))
            except ValueError:
                continue
            if cause not in (root_cause, RootCause.UNKNOWN) and cause not in contributing:
                contributing.append(cause)

        actions: list[ProposedAction] = []
        for item in payload.get("proposed_actions") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            arguments = item.get("arguments")
            actions.append(
                ProposedAction(
                    name=str(item["name"]),
                    arguments=arguments if isinstance(arguments, dict) else {},
                    rationale=str(item.get("rationale") or ""),
                )
            )

        confidence = payload.get("confidence")
        return Diagnosis(
            root_cause=root_cause,
            contributing_causes=contributing,
            confidence=min(max(float(confidence), 0.0), 1.0) if confidence is not None else 0.0,
            summary=str(payload.get("summary") or ""),
            reasoning=str(payload.get("reasoning") or ""),
            proposed_actions=actions,
            engine=f"claude:{self.settings.model}",
            tool_calls=len(tools_used),
            tools_used=list(tools_used),
        )

    def _degrade(self, request: DiagnosisRequest, reason: str) -> Diagnosis:
        """Fall back to the rule engine and record why."""
        diagnosis = self._fallback.diagnose(request)
        diagnosis.engine = f"rules (fallback: {reason})"
        request.incident.log_event("sentinel-agent", "agent.degraded", reason)
        return diagnosis


def _encode(result: dict[str, Any]) -> str:
    """Serialise a tool result, truncating pathologically large payloads."""
    text = json.dumps(result, default=str)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + '... [truncated]"}'
