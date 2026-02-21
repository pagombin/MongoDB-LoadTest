"""Retry helpers with exponential backoff + jitter."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from pymongo.errors import (
    AutoReconnect,
    ConnectionFailure,
    NetworkTimeout,
    NotPrimaryError,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from mongo_load_tester.config import RetryConfig

T = TypeVar("T")

RETRYABLE_EXCEPTIONS = (
    AutoReconnect,
    ConnectionFailure,
    NetworkTimeout,
    NotPrimaryError,
    ServerSelectionTimeoutError,
)


class RetryFailure(Exception):
    """Raised when retry loop gives up."""

    def __init__(self, operation_name: str, attempts: int, retryable: bool, last_exception: Exception):
        self.operation_name = operation_name
        self.attempts = attempts
        self.retryable = retryable
        self.last_exception = last_exception
        message = (
            f"{operation_name} failed after {attempts} attempt(s) "
            f"(retryable={retryable}): {last_exception}"
        )
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Static retry policy used by controller."""

    max_attempts: int
    initial_backoff_ms: int
    max_backoff_ms: int
    backoff_multiplier: float
    jitter_ratio: float

    @classmethod
    def from_config(cls, config: RetryConfig) -> "RetryPolicy":
        return cls(
            max_attempts=config.max_attempts,
            initial_backoff_ms=config.initial_backoff_ms,
            max_backoff_ms=config.max_backoff_ms,
            backoff_multiplier=config.backoff_multiplier,
            jitter_ratio=config.jitter_ratio,
        )

    def backoff_seconds(self, failed_attempt_index: int, rng: random.Random) -> float:
        """Compute delay after a failed attempt index (1-based)."""

        base_delay_ms = min(
            self.max_backoff_ms,
            self.initial_backoff_ms * (self.backoff_multiplier ** (failed_attempt_index - 1)),
        )
        if self.jitter_ratio > 0:
            jitter_ms = base_delay_ms * self.jitter_ratio
            base_delay_ms += rng.uniform(-jitter_ms, jitter_ms)
        return max(base_delay_ms, 0.0) / 1000.0


def is_retryable_exception(exc: Exception) -> bool:
    """Determine whether an exception is likely transient."""

    if isinstance(exc, RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, PyMongoError):
        for label in (
            "RetryableWriteError",
            "RetryableReadError",
            "TransientTransactionError",
            "ResumableChangeStreamError",
        ):
            try:
                if exc.has_error_label(label):
                    return True
            except Exception:
                continue
    return False


class RetryController(Generic[T]):
    """Executes callables with robust retry/backoff behavior."""

    def __init__(
        self,
        policy: RetryPolicy,
        rng: random.Random,
        log_warning: Callable[[str], None],
    ):
        self._policy = policy
        self._rng = rng
        self._log_warning = log_warning

    def run(self, operation_name: str, func: Callable[[], T]) -> tuple[T, int]:
        attempts = 0
        while True:
            attempts += 1
            try:
                return func(), attempts
            except Exception as exc:
                retryable = is_retryable_exception(exc)
                exhausted = attempts >= self._policy.max_attempts
                if exhausted or not retryable:
                    raise RetryFailure(
                        operation_name=operation_name,
                        attempts=attempts,
                        retryable=retryable,
                        last_exception=exc,
                    ) from exc
                delay_seconds = self._policy.backoff_seconds(attempts, self._rng)
                self._log_warning(
                    "Retry scheduled for %s (attempt %d/%d, backoff=%.3fs): %s"
                    % (operation_name, attempts + 1, self._policy.max_attempts, delay_seconds, exc)
                )
                time.sleep(delay_seconds)
