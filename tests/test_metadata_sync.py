from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from mongo_load_tester.config import AppConfig
from mongo_load_tester.metadata.store import ConfigRecord
from mongo_load_tester.metadata.sync import ConfigSyncService


@dataclass
class _FakeStore:
    record: ConfigRecord | None
    events: list[dict]

    def fetch_config(self) -> ConfigRecord | None:
        return self.record

    def append_event(self, event_type: str, message: str, payload: dict | None = None, level: str = "INFO") -> None:
        self.events.append(
            {
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
                "level": level,
            }
        )


@dataclass
class _FakeController:
    applied_versions: list[int]

    def apply_config(self, config: AppConfig, source: str = "metadata-sync") -> None:
        del source
        self.applied_versions.append(config.load.duration_seconds)


def test_config_sync_service_applies_newer_versions_only() -> None:
    base_config = AppConfig()
    record_v1 = ConfigRecord(
        load_test_id="test",
        version=1,
        updated_at=datetime.now(UTC),
        config=base_config,
    )
    store = _FakeStore(record=record_v1, events=[])
    controller = _FakeController(applied_versions=[])
    service = ConfigSyncService(metadata_store=store, controller=controller, interval_seconds=30)

    service.sync_once()
    assert len(controller.applied_versions) == 1
    assert service.last_applied_version == 1

    # Same version should be ignored.
    service.sync_once()
    assert len(controller.applied_versions) == 1

    # Higher version should apply.
    updated_config = base_config.model_copy(deep=True)
    updated_config.load.duration_seconds = 999
    store.record = ConfigRecord(
        load_test_id="test",
        version=2,
        updated_at=datetime.now(UTC),
        config=updated_config,
    )
    service.sync_once()
    assert len(controller.applied_versions) == 2
    assert controller.applied_versions[-1] == 999
    assert service.last_applied_version == 2
