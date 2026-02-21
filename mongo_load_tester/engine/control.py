"""Thread-aware control facade for dashboard-driven load lifecycle."""

from __future__ import annotations

import logging
import threading

from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.runner import LoadTestRunner


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
