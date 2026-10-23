"""Logging: size-rotated files under logs/, JSON or text, with secrets redacted before writing."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path

_SECRETS: list[str] = []
_LOCK = threading.Lock()


def add_secret(value: str) -> None:
    if not value or len(value) < 3:
        return
    with _LOCK:
        if value not in _SECRETS:
            _SECRETS.append(value)
            _SECRETS.sort(key=len, reverse=True)


def redact(text: str) -> str:
    if not _SECRETS or not isinstance(text, str):
        return text
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "****")
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        record.msg = redact(message)
        record.args = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_text:
            payload["exc"] = record.exc_text
        for key in ("run_id", "event"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
        self.converter = time.gmtime


def setup(
    log_dir: Path,
    level: str = "info",
    fmt: str = "json",
    max_size_mb: int = 100,
    max_files: int = 10,
    filename: str = "ballast.log",
    console: bool = False,
) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / filename, maxBytes=max_size_mb * 1024 * 1024, backupCount=max_files, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RedactingFilter())
    root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(TextFormatter())
        stream.addFilter(RedactingFilter())
        root.addHandler(stream)
    for noisy in ("pymongo", "aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("ballast")


def tail(log_dir: Path, filename: str = "ballast.log", lines: int = 200, min_level: str = "warning") -> list[dict]:
    path = log_dir / filename
    if not path.exists():
        return []
    order = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
    threshold = order.get(min_level, 2)
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 512 * 1024))
        data = handle.read().decode("utf-8", errors="replace").splitlines()
    out: list[dict] = []
    for line in data:
        try:
            item = json.loads(line)
        except ValueError:
            item = {"msg": line, "level": "info"}
        if order.get(item.get("level", "info"), 1) >= threshold:
            out.append(item)
    return out[-lines:]
