
"""Central logging configuration for the POC.

Provides structured JSON logging so every orchestrator action, manager
operation, and tool execution produces a parseable log record.

Format
------
Each record includes:
  timestamp   — ISO-8601 UTC (asctime)
  level       — DEBUG / INFO / WARNING / ERROR / CRITICAL
  logger      — module path (e.g. app.agents.relationship_manager)
  message     — human-readable event description
  extra.*     — structured key-value pairs added via extra={...}

Usage
-----
  logger.info("Agent started", extra={"customer_id": cid, "iteration": i})
  logger.warning("Token budget exceeded", extra={"tokens": n, "budget": b})
  logger.debug("DebugInfoManager.add called", extra={"key": k})
"""

from __future__ import annotations

import logging
import logging.config

from app.settings import Settings

# Modules whose DEBUG logs are suppressed to avoid noise from third-party libs
_NOISY_LOGGERS: list[str] = [
    "uvicorn",
    "uvicorn.access",
    "fastapi",
    "httpx",
    "httpcore",
]


def configure_logging() -> None:
    """Configure application-wide structured logging once at startup.

    Reads LOG_LEVEL from the environment via Settings.
    Sets a single StreamHandler (stdout) with a rich format that includes
    the module name and any structured extra fields.
    """
    settings = Settings.from_env()
    log_level = getattr(logging, settings.log_level, logging.DEBUG)

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "structured": {
                    "format": (
                        "%(asctime)s  %(levelname)-8s  %(short_name)-30s  "
                        "%(message)s  %(extra_fields)s"
                    ),
                    "()": _StructuredFormatter,
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "structured",
                }
            },
            "root": {
                "level": log_level,
                "handlers": ["console"],
            },
            "loggers": {
                # Application namespace — inherit root level
                "app": {"level": log_level, "propagate": True},
                # Silence noisy third-party loggers at WARNING unless DEBUG requested
                **{
                    name: {
                        "level": logging.WARNING if log_level > logging.DEBUG else logging.DEBUG,
                        "propagate": True,
                    }
                    for name in _NOISY_LOGGERS
                },
            },
        }
    )

    logging.getLogger(__name__).info(
        "Logging configured",
        extra={"log_level": settings.log_level, "app_env": settings.app_env},
    )


class _StructuredFormatter(logging.Formatter):
    """Custom formatter that appends extra key-value pairs as structured fields.

    Any key-value pairs passed via logger.xxx(..., extra={...}) are appended to
    the log line as  key=value  pairs after the message, making them easy to
    grep, parse, or ingest into a log aggregator.
    """

    # Keys that are always present on a LogRecord and should not be repeated
    _STANDARD_KEYS: frozenset[str] = frozenset(
        {
            "name",
            "short_name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "asctime",
            "extra_fields",  # injected by this formatter
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        """Format a log record, shortening logger name and appending structured extra fields."""
        # Shorten: strip "app." prefix so "app.agents.portfolio_agent" → "agents.portfolio_agent"
        name = record.name
        if name.startswith("app."):
            name = name[4:]
        record.short_name = name[:30]

        extra_items = {
            k: v
            for k, v in record.__dict__.items()
            if k not in self._STANDARD_KEYS
        }
        record.extra_fields = (
            "  ".join(f"{k}={v!r}" for k, v in sorted(extra_items.items()))
            if extra_items
            else ""
        )
        return super().format(record)
