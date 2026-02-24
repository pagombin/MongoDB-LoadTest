"""Background persistence for per-run timeline metrics."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.metadata.store import MetadataStore


class RunMetricsSyncService:
    """Persists timeline metrics to metadata per active run."""

    def __init__(
        self,
        metadata_store: MetadataStore,
        metrics_store: MetricsStore,
        interval_seconds: int = 1,
        logger: logging.Logger | None = None,
    ):
        self._metadata_store = metadata_store
        self._metrics_store = metrics_store
        self._interval_seconds = max(int(interval_seconds), 1)
        self._logger = logger or logging.getLogger("mongo_load_tester.metadata.metrics_sync")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_persisted_epoch_by_run: dict[str, int] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="metadata-metrics-sync")
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread: threading.Thread | None
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def flush_once(self) -> int:
        run_id = self._metadata_store.active_run_id()
        if not run_id:
            return 0
        rows = self._metrics_store.export_timeline_rows()
        if not rows:
            return 0
        last_epoch = self._last_persisted_epoch_by_run.get(run_id, -1)
        rows_to_persist: list[dict] = []
        max_epoch = last_epoch
        for row in rows:
            timestamp_utc = str(row.get("timestamp_utc", "")).strip()
            if not timestamp_utc:
                continue
            row_epoch = _epoch_from_timestamp_utc(timestamp_utc)
            if row_epoch <= last_epoch:
                continue
            rows_to_persist.append(row)
            if row_epoch > max_epoch:
                max_epoch = row_epoch
        if not rows_to_persist:
            return 0
        persisted_count = self._metadata_store.upsert_metric_rows(run_id, rows_to_persist)
        self._last_persisted_epoch_by_run[run_id] = max_epoch
        return persisted_count

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.flush_once()
            except Exception:  # pragma: no cover - background safety
                self._logger.exception("Metrics sync tick failed.")
            self._stop_event.wait(self._interval_seconds)


def _epoch_from_timestamp_utc(value: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())
