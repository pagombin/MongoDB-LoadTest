"""MongoDB-backed metadata store for configuration and run state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from mongo_load_tester.config import AppConfig, MongoConnectionConfig
from mongo_load_tester.engine.client import build_client
from mongo_load_tester.models import MetricsSnapshot

CONFIGS_COLLECTION = "load_test_configs"
EVENTS_COLLECTION = "load_test_events"
RUNS_COLLECTION = "load_test_runs"


@dataclass(frozen=True, slots=True)
class ConfigRecord:
    """Current persisted config state for a load test id."""

    load_test_id: str
    version: int
    updated_at: datetime
    config: AppConfig


class MetadataStore:
    """Persists load-test configuration, events, and runs."""

    def __init__(
        self,
        mongo_config: MongoConnectionConfig,
        database_name: str,
        load_test_id: str,
        logger: logging.Logger | None = None,
    ):
        self._logger = logger or logging.getLogger("mongo_load_tester.metadata")
        self._load_test_id = load_test_id
        # Metadata traffic is light; keep a conservative pool so UI callbacks
        # do not consume many file descriptors on hosts with low ulimit values.
        metadata_client_config = mongo_config.model_copy(deep=True)
        metadata_client_config.app_name = f"{mongo_config.app_name}-metadata"
        metadata_client_config.min_pool_size = 0
        metadata_client_config.max_pool_size = min(max(mongo_config.max_pool_size, 1), 20)
        self._client = build_client(metadata_client_config)
        self._database = self._client[database_name]
        self._configs: Collection = self._database[CONFIGS_COLLECTION]
        self._events: Collection = self._database[EVENTS_COLLECTION]
        self._runs: Collection = self._database[RUNS_COLLECTION]
        self._ensure_indexes()

    @property
    def load_test_id(self) -> str:
        return self._load_test_id

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - defensive close
            self._logger.exception("Failed to close metadata client.")

    def get_or_create_config(self, default_config: AppConfig) -> ConfigRecord:
        existing = self.fetch_config()
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        payload = default_config.model_dump(mode="json", by_alias=True)
        document = {
            "load_test_id": self._load_test_id,
            "version": 1,
            "config": payload,
            "created_at": now,
            "updated_at": now,
            "last_source": "bootstrap",
            "last_reason": "seed-default-config",
        }
        try:
            self._configs.insert_one(document)
            self.append_event(
                event_type="config_initialized",
                message="Metadata config initialized from bootstrap defaults.",
                payload={"version": 1},
            )
            return ConfigRecord(
                load_test_id=self._load_test_id,
                version=1,
                updated_at=now,
                config=default_config,
            )
        except DuplicateKeyError:
            current = self.fetch_config()
            if current is not None:
                return current
            raise

    def fetch_config(self) -> ConfigRecord | None:
        document = self._configs.find_one({"load_test_id": self._load_test_id})
        if not document:
            return None
        return self._parse_config_document(document)

    def save_config(self, config: AppConfig, source: str, reason: str) -> ConfigRecord:
        now = datetime.now(UTC)
        payload = config.model_dump(mode="json", by_alias=True)
        updated = self._configs.find_one_and_update(
            {"load_test_id": self._load_test_id},
            {
                "$setOnInsert": {"created_at": now, "load_test_id": self._load_test_id},
                "$set": {
                    "config": payload,
                    "updated_at": now,
                    "last_source": source,
                    "last_reason": reason,
                },
                "$inc": {"version": 1},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            raise RuntimeError(
                f"Failed to upsert config for load_test_id={self._load_test_id}"
            )
        record = self._parse_config_document(updated)
        self.append_event(
            event_type="config_updated",
            message=f"Configuration updated from {source}.",
            payload={"version": record.version, "reason": reason},
        )
        return record

    def append_event(
        self,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
        level: str = "INFO",
    ) -> None:
        now = datetime.now(UTC)
        self._events.insert_one(
            {
                "load_test_id": self._load_test_id,
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
                "level": level,
                "created_at": now,
            }
        )

    def record_run_started(self, config_version: int) -> str:
        now = datetime.now(UTC)
        run_id = str(uuid4())
        self._runs.insert_one(
            {
                "load_test_id": self._load_test_id,
                "run_id": run_id,
                "status": "running",
                "config_version": config_version,
                "started_at": now,
                "updated_at": now,
            }
        )
        self.append_event(
            event_type="run_started",
            message="Load test run started from dashboard.",
            payload={"run_id": run_id, "config_version": config_version},
        )
        return run_id

    def record_run_finished(
        self,
        run_id: str,
        status: str,
        snapshot: MetricsSnapshot,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        result = self._runs.update_one(
            {"load_test_id": self._load_test_id, "run_id": run_id},
            {
                "$set": {
                    "status": status,
                    "updated_at": now,
                    "finished_at": now,
                    "summary": {
                        "total_ops": snapshot.total_ops,
                        "success_ops": snapshot.success_ops,
                        "error_ops": snapshot.error_ops,
                        "retried_ops": snapshot.retried_ops,
                        "ops_per_second": snapshot.ops_per_second,
                        "avg_latency_ms": snapshot.avg_latency_ms,
                        "p50_latency_ms": snapshot.p50_latency_ms,
                        "p95_latency_ms": snapshot.p95_latency_ms,
                        "p99_latency_ms": snapshot.p99_latency_ms,
                        "avg_attempts_per_op": snapshot.avg_attempts_per_op,
                        "elapsed_seconds": snapshot.elapsed_seconds,
                        "operation_counts": snapshot.operation_counts,
                        "operation_latency_avg_ms": snapshot.operation_latency_avg_ms,
                        "operation_latency_p95_ms": snapshot.operation_latency_p95_ms,
                        "operation_latency_p99_ms": snapshot.operation_latency_p99_ms,
                        "error_categories": snapshot.error_categories,
                    },
                    "error": error,
                }
            },
        )
        if result.matched_count == 0:
            self._logger.warning(
                "record_run_finished found no matching run document for run_id=%s; "
                "the run record may have been deleted externally.",
                run_id,
            )
        self.append_event(
            event_type="run_finished",
            message=f"Load test run finished with status={status}.",
            payload={"run_id": run_id, "status": status, "error": error},
            level="ERROR" if error else "INFO",
        )

    def reconcile_incomplete_runs(self, reason: str = "startup-reconcile") -> int:
        """Mark previously running runs as interrupted."""

        now = datetime.now(UTC)
        result = self._runs.update_many(
            {"load_test_id": self._load_test_id, "status": "running"},
            {
                "$set": {
                    "status": "interrupted",
                    "updated_at": now,
                    "finished_at": now,
                    "error": "Run ended unexpectedly before completion metadata was recorded.",
                }
            },
        )
        count = int(result.modified_count)
        if count > 0:
            self.append_event(
                event_type="run_reconciled",
                message=f"Reconciled {count} stale running run(s) as interrupted.",
                payload={"count": count, "reason": reason},
                level="WARNING",
            )
        return count

    def _ensure_indexes(self) -> None:
        try:
            self._configs.create_index("load_test_id", unique=True)
            self._events.create_index([("load_test_id", 1), ("created_at", -1)])
            self._runs.create_index([("load_test_id", 1), ("started_at", -1)])
            self._runs.create_index("run_id")
        except Exception:
            self._logger.warning(
                "Failed to create metadata indexes for load_test_id=%s in database=%s "
                "(insufficient privileges?). The application will continue but queries may be slower.",
                self._load_test_id,
                self._database.name,
                exc_info=True,
            )

    def _parse_config_document(self, document: dict[str, Any]) -> ConfigRecord:
        raw_config = document.get("config")
        if not raw_config:
            self._logger.warning(
                "Stored config document for load_test_id=%s has missing or empty "
                "'config' field; falling back to defaults.",
                self._load_test_id,
            )
            raw_config = {}
        try:
            config = AppConfig.model_validate(raw_config)
        except Exception as exc:
            self._logger.error(
                "Failed to parse stored config for load_test_id=%s; "
                "falling back to defaults. Error: %s",
                self._load_test_id,
                exc,
            )
            config = AppConfig()
        updated_at_raw = document.get("updated_at")
        if isinstance(updated_at_raw, datetime):
            updated_at = updated_at_raw
        else:
            updated_at = datetime.now(UTC)
        return ConfigRecord(
            load_test_id=str(document.get("load_test_id", self._load_test_id)),
            version=int(document.get("version", 1)),
            updated_at=updated_at,
            config=config,
        )
