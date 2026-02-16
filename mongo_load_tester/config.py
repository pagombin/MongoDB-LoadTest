"""Configuration models and loaders."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class MongoConnectionConfig(BaseModel):
    """MongoDB client connection options."""

    uri: str = Field(default="mongodb://localhost:27017", description="MongoDB connection string")
    app_name: str = Field(default="mongo-load-tester")
    max_pool_size: int = Field(default=300, ge=1)
    min_pool_size: int = Field(default=10, ge=0)
    connect_timeout_ms: int = Field(default=8_000, ge=100)
    socket_timeout_ms: int = Field(default=120_000, ge=1_000)
    server_selection_timeout_ms: int = Field(default=8_000, ge=100)
    retry_writes: bool = True
    tls: bool = False

    model_config = ConfigDict(extra="forbid")


class NamespaceConfig(BaseModel):
    """Target namespace spread for load."""

    database_prefix: str = "loadtest_db"
    collection_prefix: str = "orders"
    databases: int = Field(default=1, ge=1)
    collections_per_database: int = Field(default=1, ge=1)

    model_config = ConfigDict(extra="forbid")


class SchemaFieldConfig(BaseModel):
    """A generated document field definition."""

    name: str
    field_type: Literal["string", "int", "float", "bool", "datetime"] = "string"
    length: int = Field(default=16, ge=1)
    min_value: float = 0
    max_value: float = 10_000
    choices: list[str] | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_bounds(self) -> "SchemaFieldConfig":
        if self.min_value > self.max_value:
            raise ValueError("min_value must be <= max_value")
        return self


class SchemaConfig(BaseModel):
    """Definition for random payload generation."""

    fields: list[SchemaFieldConfig] = Field(
        default_factory=lambda: [
            SchemaFieldConfig(name="tenant_id", field_type="string", length=8),
            SchemaFieldConfig(name="order_total", field_type="float", min_value=10, max_value=5000),
            SchemaFieldConfig(name="status", field_type="string", choices=["new", "paid", "shipped", "cancelled"]),
            SchemaFieldConfig(name="priority", field_type="int", min_value=1, max_value=5),
            SchemaFieldConfig(name="is_gift", field_type="bool"),
            SchemaFieldConfig(name="updated_at", field_type="datetime"),
        ]
    )

    model_config = ConfigDict(extra="forbid")


class OperationMixConfig(BaseModel):
    """Operation percentages for OLTP-like traffic."""

    read: float = Field(default=0.7, ge=0)
    insert: float = Field(default=0.2, ge=0)
    update: float = Field(default=0.08, ge=0)
    delete: float = Field(default=0.02, ge=0)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def ensure_positive_sum(self) -> "OperationMixConfig":
        total = self.read + self.insert + self.update + self.delete
        if total <= 0:
            raise ValueError("Operation mix must have at least one positive weight")
        return self

    def normalized(self) -> dict[str, float]:
        total = self.read + self.insert + self.update + self.delete
        return {
            "read": self.read / total,
            "insert": self.insert / total,
            "update": self.update / total,
            "delete": self.delete / total,
        }


class UserLoadConfig(BaseModel):
    """Concurrency, pacing, and runtime controls."""

    virtual_users: int = Field(default=200, ge=1)
    worker_threads: int = Field(default=80, ge=1)
    duration_seconds: int = Field(default=300, ge=1)
    ramp_up_seconds: int = Field(default=30, ge=0)
    think_time_ms_min: int = Field(default=0, ge=0)
    think_time_ms_max: int = Field(default=25, ge=0)
    target_ops_per_user_per_sec: float | None = Field(default=None, gt=0)
    initial_seed_per_collection: int = Field(default=200, ge=0)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_think_time(self) -> "UserLoadConfig":
        if self.think_time_ms_min > self.think_time_ms_max:
            raise ValueError("think_time_ms_min must be <= think_time_ms_max")
        return self


class DashboardConfig(BaseModel):
    """Dashboard runtime options."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8050, ge=1, le=65535)
    refresh_interval_ms: int = Field(default=1_000, ge=250)
    history_window_seconds: int = Field(default=600, ge=60)

    model_config = ConfigDict(extra="forbid")


class ExportConfig(BaseModel):
    """Result export options."""

    output_dir: str = "artifacts"
    default_png_width: int = Field(default=1500, ge=320)
    default_png_height: int = Field(default=900, ge=240)

    model_config = ConfigDict(extra="forbid")


class AppConfig(BaseModel):
    """Root configuration object."""

    mongo: MongoConnectionConfig = Field(default_factory=MongoConnectionConfig)
    namespace: NamespaceConfig = Field(default_factory=NamespaceConfig)
    schema: SchemaConfig = Field(default_factory=SchemaConfig)
    operation_mix: OperationMixConfig = Field(default_factory=OperationMixConfig)
    load: UserLoadConfig = Field(default_factory=UserLoadConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    random_seed: int = Field(default=42, ge=0)

    model_config = ConfigDict(extra="forbid")

    @field_validator("random_seed")
    @classmethod
    def validate_seed(cls, value: int) -> int:
        if value >= 2**32:
            raise ValueError("random_seed must be < 2^32")
        return value


def load_config(config_path: str | Path) -> AppConfig:
    """Load and validate YAML config."""

    path = Path(config_path)
    raw: dict[str, Any]
    with path.open("r", encoding="utf-8") as file_obj:
        parsed = yaml.safe_load(file_obj)
    raw = parsed or {}
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        message = f"Invalid configuration at {path}: {exc}"
        raise ValueError(message) from exc


def default_config() -> AppConfig:
    """Build a default config object."""

    return AppConfig()

