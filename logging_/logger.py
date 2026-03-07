"""
Structured logging setup for the trading system.

Uses structlog for JSON-formatted, machine-readable logs with rotation.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import structlog

from core.config import LoggingConfig


def setup_logging(config: LoggingConfig) -> None:
    """Configure structured logging with file rotation."""
    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, config.level.upper(), logging.INFO)
    max_bytes = config.rotation_mb * 1024 * 1024

    # ─── File handlers for separate log domains ───
    handlers = []

    # Main system log
    system_handler = RotatingFileHandler(
        log_dir / "system.log",
        maxBytes=max_bytes,
        backupCount=5,
    )
    system_handler.setLevel(level)
    handlers.append(system_handler)

    # Trade-specific log
    trade_handler = RotatingFileHandler(
        log_dir / "trades.log",
        maxBytes=max_bytes,
        backupCount=10,
    )
    trade_handler.setLevel(level)

    # Risk log
    risk_handler = RotatingFileHandler(
        log_dir / "risk.log",
        maxBytes=max_bytes,
        backupCount=5,
    )
    risk_handler.setLevel(level)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    handlers.append(console_handler)

    # ─── Configure root logger ───
    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=handlers,
        force=True,
    )

    # ─── Configure structlog ───
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if config.structured_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
