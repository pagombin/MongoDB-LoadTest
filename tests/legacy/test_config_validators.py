"""Tests for config model validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mongo_load_tester.config import MetadataConfig, MongoConnectionConfig, SchemaFieldConfig


def test_mongo_connection_config_valid_when_min_le_max() -> None:
    config = MongoConnectionConfig(min_pool_size=10, max_pool_size=300)
    assert config.min_pool_size == 10
    assert config.max_pool_size == 300


def test_mongo_connection_config_valid_when_min_equals_max() -> None:
    config = MongoConnectionConfig(min_pool_size=50, max_pool_size=50)
    assert config.min_pool_size == 50
    assert config.max_pool_size == 50


def test_mongo_connection_config_raises_when_min_gt_max() -> None:
    with pytest.raises(ValidationError):
        MongoConnectionConfig(min_pool_size=100, max_pool_size=50)


def test_metadata_config_valid_assignment_works() -> None:
    config = MetadataConfig()
    config.database_name = "my_metadata_db"
    config.load_test_id = "test-run-1"
    config.sync_interval_seconds = 60
    assert config.database_name == "my_metadata_db"
    assert config.load_test_id == "test-run-1"
    assert config.sync_interval_seconds == 60


def test_metadata_config_assignment_rejects_empty_string() -> None:
    config = MetadataConfig()
    with pytest.raises(ValidationError):
        config.database_name = ""
    with pytest.raises(ValidationError):
        config.load_test_id = "   "


def test_metadata_config_assignment_enforces_sync_interval_bounds() -> None:
    config = MetadataConfig()
    with pytest.raises(ValidationError):
        config.sync_interval_seconds = 4
    with pytest.raises(ValidationError):
        config.sync_interval_seconds = 3601


def test_schema_field_config_rejects_min_value_gt_max_value() -> None:
    with pytest.raises(ValidationError):
        SchemaFieldConfig(name="amount", min_value=100, max_value=50)
