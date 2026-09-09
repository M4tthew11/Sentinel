"""Anomaly detection over the pipeline's own metrics.

Two complementary detectors run on every metric vector:

1. **Seasonal robust z-score** - per metric, compare today against the median of
   the same weekday in the recent past, scaled by the median absolute deviation.
   Weekday matters: retail volume on a Saturday is not a Tuesday anomaly, and a
   naive global mean flags every weekend.

2. **IsolationForest** - a multivariate view over the whole metric vector. It
   catches combinations that look fine one metric at a time: row count normal but
   average value up and total value up is a unit-of-measure change, not a good day.

Both are deliberately unsupervised. Labelled pipeline incidents are exactly what a
new platform does not have, and a detector that needs them never ships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from sentinel.config import DetectionSettings, get_settings
from sentinel.elt.warehouse import Warehouse
from sentinel.generator.source import DATASETS
from sentinel.logging import get_logger
from sentinel.quality import metrics as m

log = get_logger(__name__)

#: Scale factor that makes the MAD a consistent estimator of sigma for normal data.
MAD_TO_SIGMA = 0.67449

#: Cap for the reported z-score, so a zero-variance metric does not produce inf.
MAX_ZSCORE = 99.0

#: Minimum same-weekday observations before the seasonal baseline is used.
MIN_SEASONAL_POINTS = 3

#: Lower bound on the robust scale, as a share of the metric's own level.
RELATIVE_SCALE_FLOOR = 0.02


def monitored_metrics(dataset: str) -> list[str]:
    """Metrics the detector watches for a dataset.

    Null rates, duplicate rates and missing columns are intentionally excluded:
    those have absolute contracts and are already covered deterministically by
    ``sentinel.quality.checks``. The detector exists for the residual - volume and
    value behaviour - where "correct" is only definable relative to history.
    """
    spec = DATASETS[dataset]
    names = [m.ROW_COUNT]
    names.extend(f"{m.MEASURE_SUM_PREFIX}{measure}" for measure in spec.measures)
    names.extend(f"{m.MEASURE_AVG_PREFIX}{measure}" for measure in spec.measures)
    return names


@dataclass(frozen=True)
class AnomalySignal:
    """One metric that deviates from its own history."""

    dataset: str
    metric: str
    observed: float
    expected: float
    zscore: float
    ratio: float
    """Observed divided by expected; 0 means the metric collapsed."""

    @property
    def direction(self) -> str:
        """Whether the metric rose or fell."""
        return "increase" if self.observed > self.expected else "decrease"

    def describe(self) -> str:
        """One-line, human-readable summary used in prompts and incident text."""
        change = (self.ratio - 1.0) * 100.0
        return (
            f"{self.dataset}.{self.metric}: observed {self.observed:,.2f} vs expected "
            f"{self.expected:,.2f} ({change:+.1f}%, robust z={self.zscore:.1f})"
        )

    def as_dict(self) -> dict[str, object]:
        """Serialisable form."""
        return {
            "dataset": self.dataset,
            "metric": self.metric,
            "observed": self.observed,
            "expected": self.expected,
            "zscore": round(self.zscore, 3),
            "ratio": round(self.ratio, 4),
            "direction": self.direction,
        }


@dataclass
class DetectionResult:
    """The detector's verdict for one dataset on one business date."""

    dataset: str
    business_date: date
    signals: list[AnomalySignal] = field(default_factory=list)
    multivariate_flag: bool = False
    multivariate_score: float | None = None
    history_runs: int = 0
    skipped_reason: str | None = None

    @property
    def is_anomalous(self) -> bool:
        """Whether anything fired."""
        return bool(self.signals) or self.multivariate_flag

    def as_dict(self) -> dict[str, object]:
        """Serialisable form."""
        return {
            "dataset": self.dataset,
            "business_date": self.business_date.isoformat(),
            "signals": [signal.as_dict() for signal in self.signals],
            "multivariate_flag": self.multivariate_flag,
            "multivariate_score": self.multivariate_score,
            "history_runs": self.history_runs,
            "skipped_reason": self.skipped_reason,
        }


def _baseline(history: pd.Series, target: date) -> tuple[float, float]:
    """Return (expected, scale) for a metric, preferring the same weekday.

    ``scale`` is a MAD-derived robust standard deviation, floored to a small
    fraction of the level so that a metric with a naturally tiny spread does not
    generate a z-score of 40 for a 2% move.
    """
    same_weekday = history[history.index.weekday == target.weekday()]  # type: ignore[attr-defined]
    sample = same_weekday if len(same_weekday) >= MIN_SEASONAL_POINTS else history

    expected = float(np.median(sample.to_numpy()))
    deviations = np.abs(sample.to_numpy() - expected)
    mad = float(np.median(deviations))
    scale = mad / MAD_TO_SIGMA

    # Two floors, because pipeline metrics live on very different scales.
    # A relative floor stops a metric with an unusually tight spread from
    # scoring a 2% move as a 5-sigma event. A Poisson floor (sqrt of the level)
    # covers count-like metrics, where a few percent of drift is ordinary
    # sampling noise - without it, row counts alarm on healthy days.
    floor = max(abs(expected) * RELATIVE_SCALE_FLOOR, float(np.sqrt(abs(expected))), 1e-6)
    return expected, max(scale, floor)


def detect_anomalies(
    history: pd.DataFrame,
    current: dict[str, float],
    dataset: str,
    business_date: date,
    settings: DetectionSettings | None = None,
) -> DetectionResult:
    """Score one metric vector against its history.

    Args:
        history: Wide metric history (index = business date, columns = metrics),
            strictly earlier than ``business_date``.
        current: The metric vector being scored.
        dataset: Dataset name, for labelling.
        business_date: The date being scored.
        settings: Detection thresholds.

    Returns:
        A :class:`DetectionResult`; ``skipped_reason`` is set when there is not
        enough history to say anything responsible.
    """
    settings = settings or get_settings().detection
    result = DetectionResult(dataset=dataset, business_date=business_date)

    watched = [name for name in monitored_metrics(dataset) if name in current]
    if history.empty or not watched:
        result.skipped_reason = "no metric history yet"
        return result

    usable = [name for name in watched if name in history.columns]
    frame = history[usable].dropna()
    result.history_runs = len(frame)
    if len(frame) < settings.min_history_runs:
        result.skipped_reason = (
            f"only {len(frame)} historical runs, {settings.min_history_runs} required"
        )
        return result

    for metric in usable:
        observed = float(current[metric])
        expected, scale = _baseline(frame[metric], business_date)
        zscore = min(abs(observed - expected) / scale, MAX_ZSCORE)
        if zscore >= settings.zscore_threshold:
            ratio = observed / expected if expected else 0.0
            result.signals.append(
                AnomalySignal(
                    dataset=dataset,
                    metric=metric,
                    observed=observed,
                    expected=expected,
                    zscore=zscore,
                    ratio=ratio,
                )
            )

    result.multivariate_flag, result.multivariate_score = _isolation_forest_verdict(
        frame, current, usable, settings
    )

    log.debug(
        "detection.scored",
        dataset=dataset,
        business_date=str(business_date),
        signals=len(result.signals),
        multivariate=result.multivariate_flag,
    )
    return result


def _isolation_forest_verdict(
    frame: pd.DataFrame,
    current: dict[str, float],
    metrics: list[str],
    settings: DetectionSettings,
) -> tuple[bool, float | None]:
    """Fit an IsolationForest on history and score the current vector.

    Metrics are normalised by their historical median before fitting so that
    row counts (hundreds) and monetary sums (tens of thousands) contribute
    comparably instead of the largest-magnitude metric dominating the split.
    """
    medians = frame[metrics].median()
    safe_medians = medians.replace(0.0, np.nan)

    train = (frame[metrics] / safe_medians).fillna(0.0).to_numpy()
    point = np.array(
        [
            [
                current[metric] / safe_medians[metric] if safe_medians[metric] else 0.0
                for metric in metrics
            ]
        ]
    )
    point = np.nan_to_num(point, nan=0.0, posinf=0.0, neginf=0.0)

    model = IsolationForest(
        n_estimators=200,
        contamination=settings.contamination,
        random_state=42,
    )
    model.fit(train)
    prediction = int(model.predict(point)[0])
    score = float(model.score_samples(point)[0])
    return prediction == -1, round(score, 4)


def detect_for_run(
    warehouse: Warehouse,
    business_date: date,
    dataset: str,
    settings: DetectionSettings | None = None,
) -> DetectionResult:
    """Load history and the current vector from the warehouse, then score."""
    history = m.metric_history(warehouse, dataset, business_date)
    current = m.latest_metrics(warehouse, dataset, business_date)
    if not current:
        result = DetectionResult(dataset=dataset, business_date=business_date)
        result.skipped_reason = "no metrics recorded for this run"
        return result
    return detect_anomalies(history, current, dataset, business_date, settings)
