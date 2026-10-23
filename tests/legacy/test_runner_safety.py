from __future__ import annotations

from mongo_load_tester.config import AppConfig
from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.runner import LoadTestRunner


def test_runner_captures_top_level_exception_without_raising(monkeypatch) -> None:
    def failing_build_client(_config):
        raise RuntimeError("simulated connect failure")

    monkeypatch.setattr("mongo_load_tester.engine.runner.build_client", failing_build_client)
    config = AppConfig()
    metrics = MetricsStore(history_window_seconds=config.dashboard.history_window_seconds)
    runner = LoadTestRunner(config=config, metrics=metrics)

    # Should not raise even though build_client fails.
    runner.run()

    assert runner.last_error is not None
    assert "simulated connect failure" in runner.last_error
    assert runner.is_running is False
    assert runner.finished_at is not None
