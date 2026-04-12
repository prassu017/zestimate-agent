"""Structured logging via structlog.

Usage:
    from zestimate_agent.logging import get_logger, configure_logging

    configure_logging()  # call once at process startup
    log = get_logger(__name__)
    log.info("fetched", url=url, status=200)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from zestimate_agent.config import get_settings

_configured = False


def configure_logging() -> None:
    """Configure structlog once per process."""
    global _configured
    if _configured:
        return

    settings = get_settings()

    # Standard library logging setup (structlog pipes through this)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=settings.log_level,
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger. Auto-configures on first call."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]
