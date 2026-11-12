"""Saved eval reports, so the console can compare engines without re-running them.

A scenario replay takes minutes and, for the Claude engine, costs money. The
console should show the last result, not trigger a new one, so ``sentinel eval``
writes one JSON file per scenario and engine and the API reads them back.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sentinel.evals.harness import EvalReport
from sentinel.logging import get_logger

log = get_logger(__name__)

#: Relative to the data directory.
EVAL_REPORT_DIR = "evals"


def report_payload(report: EvalReport) -> dict[str, Any]:
    """A JSON-ready summary of one report, with its per-day cases."""
    return {
        "scenario": report.scenario,
        "engine": report.engine,
        "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "wall_seconds": round(report.wall_seconds, 1),
        "summary": {
            "detection_rate": report.detection_rate,
            "root_cause_accuracy": report.root_cause_accuracy,
            "cause_coverage": report.cause_coverage,
            "action_accuracy": report.action_accuracy,
            "harmful_rate": report.harmful_rate,
            "mean_tool_calls": report.mean_tool_calls,
            "mean_latency_ms": report.mean_latency_ms,
        },
        "cases": [
            {
                "business_date": case.business_date,
                "dataset": case.dataset,
                "expected_causes": list(case.expected_causes),
                "observed_root_cause": case.observed_root_cause,
                "observed_contributing": list(case.observed_contributing),
                "expected_actions": list(case.expected_actions),
                "proposed_actions": list(case.proposed_actions),
                "forbidden_actions": list(case.forbidden_actions),
                "root_cause_correct": case.root_cause_correct,
                "actions_correct": case.actions_correct,
                "harmful": case.harmful,
                "cause_coverage": case.cause_coverage,
                "confidence": case.confidence,
                "final_status": case.final_status,
            }
            for case in report.cases
        ],
    }


def _engine_slug(engine: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "-" for char in engine)


def save_report(report: EvalReport, directory: Path) -> Path:
    """Write the report, replacing the previous one for the same scenario and engine."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.scenario}--{_engine_slug(report.engine)}.json"
    path.write_text(json.dumps(report_payload(report), indent=2), encoding="utf-8")
    log.info("evals.saved", path=str(path))
    return path


def load_reports(directory: Path) -> list[dict[str, Any]]:
    """Every saved report, ordered by scenario then engine. Unreadable files are skipped."""
    if not directory.is_dir():
        return []
    reports: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            log.warning("evals.unreadable", path=str(path), error=str(error))
    return sorted(reports, key=lambda item: (item.get("scenario", ""), item.get("engine", "")))
