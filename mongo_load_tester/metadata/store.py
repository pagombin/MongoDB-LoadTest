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
        self._client = build_client(mongo_config)
        self._database = self._client[database_name]
        self._configs: Collection = self._database["load_test_configs"]
        self._events: Collection = self._database["load_test_events"]
        self._runs: Collection = self._database["load_test_runs"]
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
            # Another process initialized in parallel.
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
        assert updated is not None
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
        self._runs.update_one(
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
                        "p95_latency_ms": snapshot.p95_latency_ms,
                        "avg_latency_ms": snapshot.avg_latency_ms,
                    },
                    "error": error,
                }
            },
        )
        self.append_event(
            event_type="run_finished",
            message=f"Load test run finished with status={status}.",
            payload={"run_id": run_id, "status": status, "error": error},
            level="ERROR" if error else "INFO",
        )

    def _ensure_indexes(self) -> None:
        self._configs.create_index("load_test_id", unique=True)
        self._events.create_index([("load_test_id", 1), ("created_at", -1)])
        self._runs.create_index([("load_test_id", 1), ("started_at", -1)])
        self._runs.create_index("run_id")

    def _parse_config_document(self, document: dict[str, Any]) -> ConfigRecord:
        payload = document.get("config") or {}
        config = AppConfig.model_validate(payload)
        updated_at_raw = document.get("updated_at")
        if isinstance(updated_at_raw, datetime):
            updated_at = updated_at_raw
        else:
            updated_at = datetime.now(UTC)
        return ConfigRecord(
            load_test_id=str(document["load_test_id"]),
            version=int(document.get("version", 1)),
            updated_at=updated_at,
            config=config,
        )
