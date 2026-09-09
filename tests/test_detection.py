"""The detector must fire on real shifts and stay quiet on ordinary noise."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from sentinel.config import DetectionSettings
from sentinel.detection.detector import detect_anomalies, detect_for_run
from sentinel.generator.faults import FaultSpec, FaultType
from sentinel.quality import metrics as m
from sentinel.runner import refresh_run


def _history(days: int = 28, base: float = 500.0, weekend_lift: float = 1.4) -> pd.DataFrame:
    """A weekday-seasonal row-count history with mild noise."""
    rng = np.random.default_rng(7)
    index = [date(2025, 1, 6) + timedelta(days=offset) for offset in range(days)]
    values = []
    for day in index:
        seasonal = base * (weekend_lift if day.weekday() >= 4 else 1.0)
        values.append(seasonal * float(rng.normal(1.0, 0.03)))
    return pd.DataFrame({m.ROW_COUNT: values}, index=pd.to_datetime(index))


def test_cold_start_is_silent() -> None:
    result = detect_anomalies(pd.DataFrame(), {m.ROW_COUNT: 10.0}, "pos_sales", date(2025, 2, 1))
    assert not result.is_anomalous
    assert result.skipped_reason


def test_short_history_is_silent() -> None:
    history = _history(days=4)
    result = detect_anomalies(
        history, {m.ROW_COUNT: 5.0}, "pos_sales", date(2025, 1, 10), DetectionSettings()
    )
    assert not result.is_anomalous
    assert "historical runs" in (result.skipped_reason or "")


def test_normal_variation_does_not_alarm() -> None:
    history = _history()
    target = date(2025, 2, 4)  # a Tuesday, same weekday class as the baseline
    result = detect_anomalies(history, {m.ROW_COUNT: 515.0}, "pos_sales", target)
    assert not result.signals


def test_weekend_seasonality_is_not_an_anomaly() -> None:
    """A Saturday is busy by nature; comparing it to the weekly mean would alarm."""
    history = _history()
    saturday = date(2025, 2, 8)
    result = detect_anomalies(history, {m.ROW_COUNT: 700.0}, "pos_sales", saturday)
    assert not result.signals


def test_volume_collapse_is_flagged() -> None:
    history = _history()
    target = date(2025, 2, 4)
    result = detect_anomalies(history, {m.ROW_COUNT: 170.0}, "pos_sales", target)
    signals = {signal.metric: signal for signal in result.signals}
    assert m.ROW_COUNT in signals
    assert signals[m.ROW_COUNT].direction == "decrease"
    assert signals[m.ROW_COUNT].ratio < 0.5


def test_detects_a_generated_volume_drop(settings, seeded, next_day, inject) -> None:
    inject(next_day, FaultSpec("pos_sales", FaultType.VOLUME_DROP, magnitude=0.6))
    refresh_run(next_day, seeded, settings, "test", detect=False)

    result = detect_for_run(seeded, next_day, "pos_sales", settings.detection)
    assert result.is_anomalous
    assert any(signal.metric == m.ROW_COUNT for signal in result.signals)


def test_detects_a_unit_of_measure_change(settings, seeded, next_day, inject) -> None:
    """Row count is normal; only the per-row average moves. Checks cannot see this."""
    inject(
        next_day,
        FaultSpec(
            "pos_sales",
            FaultType.UNIT_OF_MEASURE,
            column="units_sold",
            metadata={"factor": "12"},
        ),
    )
    refresh_run(next_day, seeded, settings, "test", detect=False)

    result = detect_for_run(seeded, next_day, "pos_sales", settings.detection)
    moved = {signal.metric for signal in result.signals}
    assert f"{m.MEASURE_AVG_PREFIX}units_sold" in moved
    assert m.ROW_COUNT not in moved


def test_clean_day_stays_quiet(settings, seeded, next_day, inject) -> None:
    inject(next_day)
    refresh_run(next_day, seeded, settings, "test", detect=False)

    for dataset in ("pos_sales", "shipments", "inventory"):
        result = detect_for_run(seeded, next_day, dataset, settings.detection)
        assert not result.signals, f"{dataset} raised {[s.describe() for s in result.signals]}"
