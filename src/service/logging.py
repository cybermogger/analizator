from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


def configure_logging(
    directory: str | Path,
    level: str = "INFO",
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
) -> None:
    Path(directory).mkdir(parents=True, exist_ok=True)
    if max_bytes < 1 or backup_count < 1:
        raise ValueError("Log rotation limits must be positive")
    numeric = getattr(logging, level.upper(), logging.INFO)
    handler = RotatingFileHandler(
        Path(directory) / "analyzer.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)
    root.setLevel(numeric)
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, structlog.processors.add_log_level, structlog.processors.TimeStamper(fmt="iso", utc=True), structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.stdlib.LoggerFactory(), cache_logger_on_first_use=True,
    )
