"""Structured logging setup.

Console rendering for local development, JSON for containers - the same event
stream feeds both a human reading the demo and a log aggregator in production.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_configured = False


def configure_logging(level: str = "INFO", fmt: str = "console", force: bool = False) -> None:
    """Configure structlog once per process.

    Args:
        level: Standard logging level name, e.g. ``"INFO"``.
        fmt: ``"console"`` for human-readable output, ``"json"`` for machine output.
        force: Reconfigure even if logging was already set up. The CLI needs this
            because importing the package configures logging with defaults before
            the command line has been parsed.
    """
    global _configured
    if _configured and not force:
        return

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for ``name``."""
    configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
