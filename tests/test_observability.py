from __future__ import annotations

import logging

from mongo_load_tester.config import LoggingConfig
from mongo_load_tester.observability import configure_logging


def test_in_memory_log_buffer_filters_by_level() -> None:
    logger, buffer = configure_logging(
        LoggingConfig(
            level="INFO",
            enable_file_output=False,
            ui_buffer_lines=100,
        )
    )
    logger.info("system started")
    logger.error("operation failed")

    info_lines = buffer.tail(limit=20, min_level=logging.INFO)
    error_lines = buffer.tail(limit=20, min_level=logging.ERROR)

    assert any("system started" in line for line in info_lines)
    assert any("operation failed" in line for line in info_lines)
    assert all("system started" not in line for line in error_lines)
    assert any("operation failed" in line for line in error_lines)


def test_in_memory_log_buffer_tail_limit() -> None:
    logger, buffer = configure_logging(
        LoggingConfig(
            level="DEBUG",
            enable_file_output=False,
            ui_buffer_lines=10,
        )
    )
    for idx in range(8):
        logger.info("line-%d", idx)

    tail = buffer.tail(limit=3, min_level=logging.INFO)
    assert len(tail) == 3
    assert "line-5" in tail[0]
    assert "line-7" in tail[-1]
