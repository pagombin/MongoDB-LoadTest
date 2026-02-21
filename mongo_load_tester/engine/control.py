"""Thread-aware control facade for dashboard-driven load lifecycle."""

from __future__ import annotations

import logging
import threading

from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.runner import LoadTestRunner, RuntimeChangeEvent


class LoadTestController:
    """Owns the runner thread and exposes safe control operations."""

    def __init__(self, runner: LoadTestRunner, metrics_store: MetricsStore, logger: logging.Logger):
        self._runner = runner
        self._metrics_store = metrics_store
        self._logger = logger
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            thread = threading.Thread(target=self._runner.run, daemon=True, name="runner-main")
            self._thread = thread
            thread.start()
            return True

    def stop(self) -> None:
        self._runner.stop()

    def pause(self) -> bool:
        return self._runner.pause()

    def resume(self) -> bool:
        return self._runner.resume()

    def update_duration_seconds(self, duration_seconds: int) -> int:
        return self._runner.update_duration_seconds(duration_seconds)

    def update_target_virtual_users(self, virtual_users: int) -> tuple[int, int]:
        return self._runner.update_target_virtual_users(virtual_users)

    def update_worker_threads(self, worker_threads: int) -> tuple[int, int]:
        return self._runner.update_worker_threads(worker_threads)

    def update_operation_mix(
        self,
        read: float,
        insert: float,
        update: float,
        delete: float,
    ) -> dict[str, float]:
        mix = self._runner.update_operation_mix(read=read, insert=insert, update=update, delete=delete)
        return mix.normalized()

    def update_pacing(
        self,
        think_time_ms_min: int,
        think_time_ms_max: int,
        target_ops_per_user_per_sec: float | None,
    ) -> tuple[int, int, float | None]:
        return self._runner.update_pacing(
            think_time_ms_min=think_time_ms_min,
            think_time_ms_max=think_time_ms_max,
            target_ops_per_user_per_sec=target_ops_per_user_per_sec,
        )

    def update_retry_policy(
        self,
        max_attempts: int,
        initial_backoff_ms: int,
        max_backoff_ms: int,
        backoff_multiplier: float,
        jitter_ratio: float,
    ) -> tuple[int, int, int, float, float]:
        policy = self._runner.update_retry_policy(
            max_attempts=max_attempts,
            initial_backoff_ms=initial_backoff_ms,
            max_backoff_ms=max_backoff_ms,
            backoff_multiplier=backoff_multiplier,
            jitter_ratio=jitter_ratio,
        )
        return (
            policy.max_attempts,
            policy.initial_backoff_ms,
            policy.max_backoff_ms,
            policy.backoff_multiplier,
            policy.jitter_ratio,
        )

    def update_runtime_logging(
        self,
        level_name: str,
        progress_log_interval_seconds: int,
        log_operation_failures: bool,
    ) -> tuple[str, int, bool]:
        normalized_level = (level_name or "INFO").upper()
        if normalized_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("Unsupported log level. Choose DEBUG, INFO, WARNING, or ERROR.")
        numeric_level = getattr(logging, normalized_level)
        self._logger.setLevel(numeric_level)
        for handler in self._logger.handlers:
            if handler.__class__.__name__ == "InMemoryLogHandler":
                handler.setLevel(logging.DEBUG)
            else:
                handler.setLevel(numeric_level)
        interval, failures = self._runner.update_logging_controls(
            progress_log_interval_seconds=progress_log_interval_seconds,
            log_operation_failures=log_operation_failures,
        )
        self._logger.info("Runtime logger level updated to %s.", normalized_level)
        return normalized_level, interval, failures

    def live_settings_snapshot(self) -> dict[str, object]:
        think_min, think_max, target_rate = self._runner.pacing_snapshot()
        retry_policy = self._runner.retry_policy_snapshot()
        return {
            "max_virtual_users": self._runner.max_virtual_users,
            "target_virtual_users": self._runner.target_virtual_users,
            "target_worker_threads": self._runner.target_worker_threads,
            "worker_thread_pool_size": self._runner.worker_thread_pool_size,
            "target_duration_seconds": self._runner.target_duration_seconds,
            "operation_mix": self._runner.operation_mix_normalized(),
            "think_time_ms_min": think_min,
            "think_time_ms_max": think_max,
            "target_ops_per_user_per_sec": target_rate,
            "retry_policy": {
                "max_attempts": retry_policy.max_attempts,
                "initial_backoff_ms": retry_policy.initial_backoff_ms,
                "max_backoff_ms": retry_policy.max_backoff_ms,
                "backoff_multiplier": retry_policy.backoff_multiplier,
                "jitter_ratio": retry_policy.jitter_ratio,
            },
            "progress_log_interval_seconds": self._runner.progress_log_interval_seconds,
            "log_operation_failures": self._runner.log_operation_failures,
            "log_level": logging.getLevelName(self._logger.level),
        }

    def runtime_change_events(self, limit: int = 8) -> list[RuntimeChangeEvent]:
        return self._runner.runtime_change_events(limit=limit)

    def restart(self) -> tuple[bool, str]:
        self._logger.info("Restart requested from dashboard.")
        self.stop()
        thread = self._thread_ref()
        if thread is not None and thread.is_alive():
            thread.join(timeout=15)
        if thread is not None and thread.is_alive():
            message = "Restart pending: previous run is still shutting down."
            self._logger.warning(message)
            return False, message
        self._metrics_store.reset()
        started = self.start()
        if started:
            return True, "Load test restarted with existing settings. Previous metrics were cleared."
        return False, "Restart ignored because another run is already active."

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread_ref()
        if thread is not None:
            thread.join(timeout=timeout)

    def _thread_ref(self) -> threading.Thread | None:
        with self._lock:
            return self._thread
