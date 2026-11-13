"""Shared fixtures.

Every test gets its own warehouse under ``tmp_path``; nothing touches the demo
data directory, and no test needs an API key or a network.

The clean history that the detector needs is expensive to build (one pipeline run
per day), so it is built once per session into a template directory and copied
per test. Tests still get an isolated, writable warehouse - they just do not each
pay for fourteen pipeline runs.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest

from sentinel.config import AgentProvider, RemediationMode, ReviewerProvider, Settings
from sentinel.elt.warehouse import Warehouse
from sentinel.generator.faults import FaultSpec
from sentinel.generator.source import generate_day, generate_range
from sentinel.runner import refresh_run

HISTORY_START = date(2025, 3, 3)
HISTORY_DAYS = 14


def _build_settings(data_dir: Path) -> Settings:
    """Settings with the agent forced offline and a short detector warm-up.

    The reviewer is off by default so the tests of the whitelist and the risk
    gate exercise those alone; the reviewer has its own tests, which turn it on.
    """
    settings = Settings(data_dir=data_dir)
    settings.agent.provider = AgentProvider.RULES
    settings.reviewer.provider = ReviewerProvider.OFF
    settings.remediation.mode = RemediationMode.AUTO
    settings.detection.min_history_runs = 5
    settings.ensure_dirs()
    return settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at an isolated data directory."""
    return _build_settings(tmp_path / "data")


@pytest.fixture
def warehouse(settings: Settings) -> Iterator[Warehouse]:
    """An initialised, empty warehouse."""
    instance = Warehouse(settings.warehouse_path)
    instance.init_schema()
    yield instance
    instance.close()


@pytest.fixture
def history_end() -> date:
    """Last date of the shared clean history."""
    return HISTORY_START + timedelta(days=HISTORY_DAYS - 1)


@pytest.fixture(scope="session")
def history_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the clean history once per session and return its data directory."""
    data_dir = tmp_path_factory.mktemp("history") / "data"
    settings = _build_settings(data_dir)
    end = HISTORY_START + timedelta(days=HISTORY_DAYS - 1)

    generate_range(HISTORY_START, end, settings=settings)
    warehouse = Warehouse(settings.warehouse_path)
    try:
        warehouse.init_schema()
        for offset in range(HISTORY_DAYS):
            refresh_run(
                HISTORY_START + timedelta(days=offset),
                warehouse,
                settings,
                "test-history",
                detect=False,
            )
    finally:
        warehouse.close()
    return data_dir


@pytest.fixture
def seeded(settings: Settings, history_template: Path) -> Iterator[Warehouse]:
    """A private copy of the clean history, ready for a fault to be injected."""
    shutil.copytree(history_template, settings.data_dir, dirs_exist_ok=True)
    instance = Warehouse(settings.warehouse_path)
    try:
        instance.init_schema()
        yield instance
    finally:
        instance.close()


@pytest.fixture
def next_day(history_end: date) -> date:
    """The first business date after the shared history."""
    return history_end + timedelta(days=1)


@pytest.fixture
def inject(settings: Settings):
    """Generate one business date with an optional fault injected."""

    def _inject(business_date: date, spec: FaultSpec | None = None) -> None:
        faults = {spec.dataset: spec} if spec else None
        generate_day(business_date, faults, settings)

    return _inject
