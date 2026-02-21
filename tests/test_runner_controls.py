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


def test_runner_live_update_methods_sanitize_and_apply() -> None:
    config = AppConfig()
    metrics = MetricsStore(history_window_seconds=config.dashboard.history_window_seconds)
    runner = LoadTestRunner(config=config, metrics=metrics)
    base_users = config.load.virtual_users
    base_workers = config.load.worker_threads

    users, max_users = runner.update_target_virtual_users(base_users + 50)
    assert users == base_users + 50
    assert max_users == base_users + 50

    workers, pool = runner.update_worker_threads(base_workers + 7)
    assert workers == base_workers + 7
    assert pool == base_workers + 7

    mix = runner.update_operation_mix(read=8, insert=1, update=1, delete=0)
    normalized = mix.normalized()
    assert round(normalized["read"], 2) == 0.80
    assert round(normalized["insert"], 2) == 0.10

    min_ms, max_ms, target_rate = runner.update_pacing(
        think_time_ms_min=3,
        think_time_ms_max=25,
        target_ops_per_user_per_sec=12.5,
    )
    assert min_ms == 3
    assert max_ms == 25
    assert target_rate == 12.5

    policy = runner.update_retry_policy(
        max_attempts=7,
        initial_backoff_ms=25,
        max_backoff_ms=400,
        backoff_multiplier=1.5,
        jitter_ratio=0.1,
    )
    assert policy.max_attempts == 7
    assert policy.max_backoff_ms == 400

    interval, failures = runner.update_logging_controls(progress_log_interval_seconds=2, log_operation_failures=False)
    assert interval == 2
    assert failures is False

    events = runner.runtime_change_events(limit=5)
    assert events
