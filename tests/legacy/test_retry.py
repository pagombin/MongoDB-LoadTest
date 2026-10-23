from __future__ import annotations

import random

import pytest
from pymongo.errors import AutoReconnect

from mongo_load_tester.config import RetryConfig
from mongo_load_tester.engine.retry import RetryController, RetryFailure, RetryPolicy


def test_retry_controller_retries_transient_failures(monkeypatch) -> None:
    policy = RetryPolicy.from_config(
        RetryConfig(
            max_attempts=4,
            initial_backoff_ms=1,
            max_backoff_ms=8,
            backoff_multiplier=2.0,
            jitter_ratio=0.0,
        )
    )
    warnings: list[str] = []
    sleeps: list[float] = []
    controller = RetryController(policy=policy, rng=random.Random(7), log_warning=warnings.append)
    state = {"attempt": 0}

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("mongo_load_tester.engine.retry.time.sleep", fake_sleep)

    def flaky_op() -> str:
        state["attempt"] += 1
        if state["attempt"] < 3:
            raise AutoReconnect("transient")
        return "ok"

    value, attempts = controller.run("flaky_op", flaky_op)

    assert value == "ok"
    assert attempts == 3
    assert len(warnings) == 2
    assert sleeps == [0.001, 0.002]


def test_retry_controller_gives_up_on_non_retryable(monkeypatch) -> None:
    policy = RetryPolicy.from_config(RetryConfig(max_attempts=5))
    controller = RetryController(policy=policy, rng=random.Random(1), log_warning=lambda _: None)
    sleeps: list[float] = []
    monkeypatch.setattr("mongo_load_tester.engine.retry.time.sleep", lambda value: sleeps.append(value))

    with pytest.raises(RetryFailure) as exc_info:
        controller.run("non_retryable", lambda: (_ for _ in ()).throw(ValueError("bad input")))

    exc = exc_info.value
    assert exc.attempts == 1
    assert exc.retryable is False
    assert sleeps == []


def test_retry_controller_exhausts_retryable_failures(monkeypatch) -> None:
    policy = RetryPolicy.from_config(
        RetryConfig(
            max_attempts=3,
            initial_backoff_ms=1,
            max_backoff_ms=10,
            backoff_multiplier=2.0,
            jitter_ratio=0.0,
        )
    )
    controller = RetryController(policy=policy, rng=random.Random(1), log_warning=lambda _: None)
    sleeps: list[float] = []
    monkeypatch.setattr("mongo_load_tester.engine.retry.time.sleep", lambda value: sleeps.append(value))

    with pytest.raises(RetryFailure) as exc_info:
        controller.run("always_fails", lambda: (_ for _ in ()).throw(AutoReconnect("network")))

    exc = exc_info.value
    assert exc.attempts == 3
    assert exc.retryable is True
    assert sleeps == [0.001, 0.002]
