"""Background sync for externally-updated metadata config."""

from __future__ import annotations

import logging
import threading

from mongo_load_tester.engine.control import LoadTestController
from mongo_load_tester.metadata.store import MetadataStore


class ConfigSyncService:
    """Applies metadata config updates on a polling interval."""

    def __init__(
        self,
        metadata_store: MetadataStore,
        controller: LoadTestController,
        interval_seconds: int,
        logger: logging.Logger | None = None,
    ):
        self._metadata_store = metadata_store
        self._controller = controller
        self._interval_seconds = max(int(interval_seconds), 5)
        self._logger = logger or logging.getLogger("mongo_load_tester.metadata.sync")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_applied_version = 0

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    @property
    def last_applied_version(self) -> int:
        with self._lock:
            return self._last_applied_version

    def mark_applied_version(self, version: int) -> None:
        with self._lock:
            self._last_applied_version = max(self._last_applied_version, int(version))

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="metadata-config-sync")
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread: threading.Thread | None
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def sync_once(self) -> None:
        if self._controller.is_running:
            return
        record = self._metadata_store.fetch_config()
        if record is None:
            return
        with self._lock:
            if record.version <= self._last_applied_version:
                return
        self._controller.apply_config(record.config, source="metadata-sync")
        self.mark_applied_version(record.version)
        self._metadata_store.append_event(
            event_type="config_sync_applied",
            message=f"Applied metadata config version {record.version} from sync service.",
            payload={"version": record.version},
        )
        self._logger.info("Applied metadata config version %d.", record.version)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.sync_once()
            except Exception:  # pragma: no cover - background safety
                self._logger.exception("Metadata config sync tick failed.")
            self._stop_event.wait(self._interval_seconds)
