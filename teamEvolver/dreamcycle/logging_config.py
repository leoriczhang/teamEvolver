"""Logging configuration — structured JSON + file rotation + console output."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .config import LogConfig


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for machine-readable logs."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "job_name"):
            log_entry["job_name"] = record.job_name
        if hasattr(record, "tool_name"):
            log_entry["tool_name"] = record.tool_name
        return json.dumps(log_entry, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable colored console output."""

    COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        name = record.name.split(".")[-1][:12]
        msg = record.getMessage()
        formatted = f"{color}{timestamp} [{name:>12}] {record.levelname:<7}{self.RESET} {msg}"
        if record.exc_info and record.exc_info[0]:
            formatted += "\n" + self.formatException(record.exc_info)
        return formatted


def setup_logging(config: LogConfig) -> None:
    """Configure the logging system with both console and file handlers."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    # Clear existing handlers
    root.handlers.clear()

    # Console handler (human-readable)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    # JSON file handler (structured, rotated)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = config.log_dir / "dreamcycle.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=config.max_log_size_mb * 1024 * 1024,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JSONFormatter())
    root.addHandler(file_handler)

    # Execution trace log (separate file for detailed agent traces)
    trace_file = config.log_dir / "trace.log"
    trace_handler = logging.handlers.RotatingFileHandler(
        trace_file,
        maxBytes=config.max_log_size_mb * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    trace_handler.setLevel(logging.DEBUG)
    trace_handler.setFormatter(JSONFormatter())
    trace_logger = logging.getLogger("dreamcycle.trace")
    trace_logger.addHandler(trace_handler)
    trace_logger.propagate = False  # Don't double-log to root

    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info("Logging initialized: console=%s, file=%s", config.log_level, log_file)


def setup_embedded_logging(config: LogConfig) -> None:
    """Attach DreamCycle file logs without replacing teamEvolver handlers."""
    config.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("teamEvolver.dreamcycle")
    logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    log_file = config.log_dir / "dreamcycle.log"
    resolved = log_file.resolve()
    for handler in logger.handlers:
        if (
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename).resolve() == resolved
        ):
            return

    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=config.max_log_size_mb * 1024 * 1024,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)
