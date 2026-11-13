"""Application settings.

All settings are optional: Sentinel is designed to run end-to-end with zero
configuration so that a reviewer can clone the repository and get a working demo
in one command. Environment variables use the ``SENTINEL_`` prefix and ``__`` as
the nesting delimiter, e.g. ``SENTINEL_AGENT__PROVIDER=rules``.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentProvider(StrEnum):
    """Which reasoning engine backs the remediation agent."""

    AUTO = "auto"
    """Use Claude when an API key is present, otherwise fall back to rules."""

    CLAUDE = "claude"
    """Force the Claude tool-use agent; fails loudly if no API key is set."""

    RULES = "rules"
    """Force the deterministic rule-based engine (used in CI and offline demos)."""


class RemediationMode(StrEnum):
    """How much autonomy the agent has when applying a fix."""

    MANUAL = "manual"
    """Every action is proposed only; a human approves it via the API or CLI."""

    AUTO = "auto"
    """Low-risk actions execute immediately; risky ones still require approval."""


class AgentSettings(BaseModel):
    """Configuration for the diagnosis agent.

    Note the absence of a temperature setting: current Claude models reject
    sampling parameters, and behaviour is steered through the prompt and the
    effort level instead.
    """

    provider: AgentProvider = AgentProvider.AUTO
    model: str = "claude-opus-5"
    effort: str = "medium"
    """Reasoning effort: low | medium | high | xhigh | max."""

    max_tool_calls: int = Field(default=8, ge=1, le=32)
    max_tokens: int = Field(default=8192, ge=1024, le=32000)

    @property
    def api_key(self) -> str | None:
        """Read the Anthropic key from the environment at call time.

        Deliberately not a settings field: the key should never end up in a
        serialised settings object, a log line or an incident document.
        """
        return os.environ.get("ANTHROPIC_API_KEY") or None


class RemediationSettings(BaseModel):
    """Configuration for the remediation executor."""

    mode: RemediationMode = RemediationMode.AUTO
    max_actions_per_incident: int = Field(default=3, ge=1, le=10)


class DetectionSettings(BaseModel):
    """Thresholds for the anomaly detector."""

    min_history_runs: int = Field(default=7, ge=3)
    """Runs of history required before the statistical detector is trusted."""

    zscore_threshold: float = Field(default=3.5, gt=0)
    """Robust (MAD-based) z-score above which a metric is considered anomalous."""

    contamination: float = Field(default=0.1, gt=0, lt=0.5)
    """Expected share of anomalous runs, passed to IsolationForest."""


class Settings(BaseSettings):
    """Top-level settings object."""

    model_config = SettingsConfigDict(
        env_prefix="SENTINEL_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Path("data")
    log_level: str = "INFO"
    log_format: str = "console"

    business_date: str | None = None
    """Date (YYYY-MM-DD) for ``run``/``monitor`` when none is given as an argument."""

    business_timezone: str = "UTC"
    """IANA timezone in which "yesterday" is worked out for a scheduled run."""

    agent: AgentSettings = Field(default_factory=AgentSettings)
    remediation: RemediationSettings = Field(default_factory=RemediationSettings)
    detection: DetectionSettings = Field(default_factory=DetectionSettings)

    @property
    def landing_dir(self) -> Path:
        """Directory where the generator drops raw source files."""
        return self.data_dir / "landing"

    @property
    def warehouse_path(self) -> Path:
        """Path of the DuckDB warehouse file."""
        return self.data_dir / "warehouse.duckdb"

    def ensure_dirs(self) -> None:
        """Create the runtime directories if they do not exist yet."""
        self.landing_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests that patch the environment)."""
    get_settings.cache_clear()
