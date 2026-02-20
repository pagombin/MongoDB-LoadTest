from __future__ import annotations

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
