"""Business-date resolution for the scheduled pipeline job."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from sentinel.cli import app, resolve_business_date
from sentinel.config import Settings, reset_settings_cache

NOW = datetime(2026, 11, 13, 3, 0, tzinfo=UTC)


def test_explicit_argument_wins(settings: Settings) -> None:
    settings.business_date = "2026-01-01"
    assert resolve_business_date("2026-11-10", settings, NOW) == date(2026, 11, 10)


def test_environment_override_is_used_without_an_argument(settings: Settings) -> None:
    settings.business_date = "2026-11-09"
    assert resolve_business_date(None, settings, NOW) == date(2026, 11, 9)


def test_defaults_to_yesterday_in_utc(settings: Settings) -> None:
    assert resolve_business_date(None, settings, NOW) == date(2026, 11, 12)


def test_yesterday_follows_the_business_timezone(settings: Settings) -> None:
    # 03:00 UTC is still the evening of the 12th in Los Angeles.
    settings.business_timezone = "America/Los_Angeles"
    assert resolve_business_date(None, settings, NOW) == date(2026, 11, 11)


def test_unexpanded_placeholder_is_rejected(settings: Settings) -> None:
    with pytest.raises(typer.BadParameter, match="not a date"):
        resolve_business_date("$(BUSINESS_DATE)", settings, NOW)


def test_unknown_timezone_is_rejected(settings: Settings) -> None:
    settings.business_timezone = "Mars/Olympus"
    with pytest.raises(typer.BadParameter, match="IANA"):
        resolve_business_date(None, settings, NOW)


def test_monitor_runs_without_a_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the Container Apps job executes: `sentinel monitor`, nothing else."""
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_AGENT__PROVIDER", "rules")
    monkeypatch.setenv("SENTINEL_BUSINESS_DATE", "2025-03-03")
    reset_settings_cache()
    try:
        result = CliRunner().invoke(app, ["monitor"])
    finally:
        reset_settings_cache()
    assert result.exit_code == 0, result.output
    assert "2025-03-03" in result.output
