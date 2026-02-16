"""Logging setup + in-memory log buffer for dashboard display."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections import deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock

from mongo_load_tester.config import LoggingConfig


@dataclass(frozen=True, slots=True)
class LogLine:
    """A single UI-facing log line."""

    levelno: int
    line: str


class InMemoryLogBuffer:
    """Thread-safe fixed-size log line buffer."""

    def __init__(self, max_lines: int):
        self._lines: deque[LogLine] = deque(maxlen=max_lines)
        self._lock = Lock()

    def append(self, levelno: int, line: str) -> None:
        with self._lock:
            self._lines.append(LogLine(levelno=levelno, line=line))

    def tail(self, limit: int, min_level: int = logging.INFO) -> list[str]:
        with self._lock:
            if limit <= 0:
                return []
            filtered = [item.line for item in self._lines if item.levelno >= min_level]
            return filtered[-limit:]


class InMemoryLogHandler(logging.Handler):
    """Logging handler that mirrors records into InMemoryLogBuffer."""

    def __init__(self, buffer: InMemoryLogBuffer):
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - logging safety
        try:
            line = self.format(record)
        except Exception:
            line = record.getMessage()
        self._buffer.append(record.levelno, line)


def configure_logging(config: LoggingConfig) -> tuple[logging.Logger, InMemoryLogBuffer]:
    """Build logger with stream/file handlers and dashboard buffer."""

    level = getattr(logging, config.level.upper(), logging.INFO)
    logger = logging.getLogger("mongo_load_tester")
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(threadName)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if config.enable_file_output:
        log_file_path = Path(config.log_file_path)
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=log_file_path,
            maxBytes=config.max_file_bytes,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    buffer = InMemoryLogBuffer(max_lines=config.ui_buffer_lines)
    memory_handler = InMemoryLogHandler(buffer)
    memory_handler.setLevel(logging.DEBUG)
    memory_handler.setFormatter(formatter)
    logger.addHandler(memory_handler)

    # Keep third-party noise manageable by default.
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("dash").setLevel(logging.WARNING)
    install_global_exception_handlers(logger)
    return logger, buffer


def install_global_exception_handlers(logger: logging.Logger) -> None:
    """Install global hooks so uncaught exceptions are always logged."""

    def handle_sys_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            logger.warning("KeyboardInterrupt received; shutting down gracefully.")
            return
        logger.critical(
            "Unhandled exception reached sys.excepthook",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def handle_thread_exception(args: threading.ExceptHookArgs):
        if args.exc_type is KeyboardInterrupt:
            logger.warning("KeyboardInterrupt in thread %s", args.thread.name if args.thread else "unknown")
            return
        logger.critical(
            "Unhandled exception in thread %s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    def handle_asyncio_exception(loop: asyncio.AbstractEventLoop, context: dict):
        message = context.get("message", "Unhandled asyncio exception")
        exc = context.get("exception")
        if exc is None:
            logger.critical("%s | context=%s", message, context)
            return
        logger.critical("%s", message, exc_info=(type(exc), exc, exc.__traceback__))

    sys.excepthook = handle_sys_exception
    threading.excepthook = handle_thread_exception
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    loop.set_exception_handler(handle_asyncio_exception)
