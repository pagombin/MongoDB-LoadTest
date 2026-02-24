from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mongo_load_tester.config import AppConfig, OperationMixConfig, load_config


def test_operation_mix_normalizes_to_one() -> None:
    mix = OperationMixConfig(read=70, insert=20, update=10, delete=0)
    normalized = mix.normalized()
    assert pytest.approx(sum(normalized.values()), rel=1e-6) == 1.0
    assert normalized["read"] == pytest.approx(0.7)


def test_invalid_operation_mix_fails_load(tmp_path: Path) -> None:
    config_payload = AppConfig().model_dump(mode="json")
    config_payload["operation_mix"] = {"read": 0, "insert": 0, "update": 0, "delete": 0}
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(config_payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(path)


def test_metadata_defaults_present() -> None:
    config = AppConfig()
    assert config.metadata.database_name == "loader_metadata"
    assert config.metadata.load_test_id
    assert config.metadata.sync_interval_seconds == 30
