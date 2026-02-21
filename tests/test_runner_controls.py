from __future__ import annotations

import time

from mongo_load_tester.config import AppConfig
from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.runner import LoadTestRunner


def test_runner_control_methods_when_not_running() -> None:
    config = AppConfig()
    metrics = MetricsStore(history_window_seconds=config.dashboard.history_window_seconds)
    runner = LoadTestRunner(config=config, metrics=metrics)

    assert runner.pause() is False
    assert runner.resume() is False
    assert runner.is_paused is False
    assert runner.active_virtual_users == 0


def test_runner_duration_updates_are_sanitized() -> None:
    config = AppConfig()
    metrics = MetricsStore(history_window_seconds=config.dashboard.history_window_seconds)
    runner = LoadTestRunner(config=config, metrics=metrics)

    updated = runner.update_duration_seconds(1)
    assert updated == 1
    assert runner.target_duration_seconds == 1

    updated = runner.update_duration_seconds(-20)
    assert updated == 1
    assert runner.target_duration_seconds == 1


def test_runner_active_elapsed_freezes_after_finalization_marker() -> None:
    config = AppConfig()
    metrics = MetricsStore(history_window_seconds=config.dashboard.history_window_seconds)
    runner = LoadTestRunner(config=config, metrics=metrics)

    runner._run_started_monotonic = time.monotonic() - 100  # type: ignore[attr-defined]
    runner._final_active_elapsed_seconds = 12.5  # type: ignore[attr-defined]

    assert runner.active_elapsed_seconds() == 12.5
