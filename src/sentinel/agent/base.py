"""Engine interface and selection.

Two engines implement the same protocol:

* :class:`~sentinel.agent.rules.RuleBasedEngine` - deterministic, offline, free;
* :class:`~sentinel.agent.claude_engine.ClaudeEngine` - a Claude tool-use loop.

Both are first-class. The rule engine is not a stub: it is what runs in CI, what
runs for a reviewer with no API key, and what the LLM is scored against in the
eval harness. If an LLM cannot beat a decision tree on a task, the decision tree
is the correct thing to ship.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sentinel.config import AgentProvider, Settings, get_settings
from sentinel.elt.warehouse import Warehouse
from sentinel.incidents.models import Diagnosis, Incident
from sentinel.logging import get_logger

log = get_logger(__name__)


@dataclass
class DiagnosisRequest:
    """Everything an engine needs to diagnose one incident."""

    incident: Incident
    warehouse: Warehouse
    settings: Settings


@runtime_checkable
class DiagnosisEngine(Protocol):
    """Produces a diagnosis for an incident."""

    name: str

    def diagnose(self, request: DiagnosisRequest) -> Diagnosis:
        """Investigate the incident and return a structured conclusion."""
        ...


def build_engine(settings: Settings | None = None) -> DiagnosisEngine:
    """Pick an engine according to configuration and the available credentials."""
    settings = settings or get_settings()
    provider = settings.agent.provider
    has_key = bool(settings.agent.api_key)

    if provider is AgentProvider.RULES:
        from sentinel.agent.rules import RuleBasedEngine

        return RuleBasedEngine()

    if provider is AgentProvider.CLAUDE:
        if not has_key:
            raise RuntimeError(
                "SENTINEL_AGENT__PROVIDER=claude but ANTHROPIC_API_KEY is not set. "
                "Set the key, or use provider=auto to fall back to the rule engine."
            )
        from sentinel.agent.claude_engine import ClaudeEngine

        return ClaudeEngine(settings.agent)

    if has_key:
        from sentinel.agent.claude_engine import ClaudeEngine

        return ClaudeEngine(settings.agent)

    log.info("agent.fallback", reason="ANTHROPIC_API_KEY not set", engine="rules")
    from sentinel.agent.rules import RuleBasedEngine

    return RuleBasedEngine()
