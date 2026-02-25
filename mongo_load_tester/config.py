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
    tls: bool | None = Field(
        default=None,
        description="Optional TLS override. Keep null to use connection-string defaults.",
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_pool_sizes(self) -> "MongoConnectionConfig":
        if self.min_pool_size > self.max_pool_size:
            raise ValueError("min_pool_size must be <= max_pool_size")
        return self


class NamespaceConfig(BaseModel):
    """Target namespace spread for load."""

    database_prefix: str = "loadtest_db"
    collection_prefix: str = "orders"
    databases: int = Field(default=1, ge=1)
    collections_per_database: int = Field(default=1, ge=1)

    model_config = ConfigDict(extra="forbid")


FIELD_TYPES = (
    "string", "int", "float", "bool", "datetime",
    "objectid", "uuid", "faker", "choice", "date_range",
    "object", "array", "decimal", "null",
)
FAKER_PROVIDERS = (
    "name", "first_name", "last_name", "email", "phone_number",
    "company", "address", "city", "country", "zipcode",
    "url", "ipv4", "user_agent", "job", "text", "sentence",
    "paragraph", "word", "credit_card_number", "iban", "ssn",
    "license_plate", "color_name", "currency_code", "file_name",
)


class SchemaFieldConfig(BaseModel):
    """A generated document field definition supporting nested MongoDB structures."""

    name: str = Field(default="", description="Field name. Empty when used as array items template.")
    field_type: Literal[
        "string", "int", "float", "bool", "datetime",
        "objectid", "uuid", "faker", "choice", "date_range",
        "object", "array", "decimal", "null",
    ] = "string"
    length: int = Field(default=16, ge=1)
    prefix: str = Field(default="", description="Static prefix prepended to generated string values.")
    suffix: str = Field(default="", description="Static suffix appended to generated string values.")
    composite: Literal["none", "prefix_uuid", "uuid_suffix", "prefix_uuid_suffix", "prefix_random", "random_suffix"] = Field(
        default="none",
        description="Composite string pattern. Combines prefix/suffix with UUID or random characters.",
    )
    min_value: float = 0
    max_value: float = 10_000
    choices: list[str] | None = None
    faker_provider: str | None = Field(
        default=None,
        description="Faker method name, e.g. 'name', 'email', 'company'. Used when field_type='faker'.",
    )
    date_mode: Literal["now", "random_past", "random_future", "increment"] = Field(
        default="now",
        description="How to generate datetime/date_range values.",
    )
    date_range_days: int = Field(default=365, ge=1)
    date_increment_seconds: int = Field(default=1, ge=1)
    fields: list["SchemaFieldConfig"] | None = Field(
        default=None,
        description="Sub-fields for field_type='object' (embedded documents).",
    )
    items: "SchemaFieldConfig | None" = Field(
        default=None,
        description="Element template for field_type='array'. Defines what each array element looks like.",
    )
    min_items: int = Field(default=1, ge=0, description="Minimum array length.")
    max_items: int = Field(default=5, ge=1, description="Maximum array length.")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_field(self) -> "SchemaFieldConfig":
        if self.min_value > self.max_value:
            raise ValueError(f"min_value must be <= max_value (field '{self.name}')")
        if self.field_type == "faker" and not self.faker_provider:
            raise ValueError(f"faker_provider is required when field_type='faker' (field '{self.name}')")
        if self.field_type == "choice" and not self.choices:
            raise ValueError(f"choices list is required when field_type='choice' (field '{self.name}')")
        if self.field_type == "object":
            if not self.fields:
                raise ValueError(f"fields list is required when field_type='object' (field '{self.name}')")
            self._validate_no_duplicate_names(self.fields, self.name)
        if self.field_type == "array":
            if self.items is None:
                raise ValueError(f"items template is required when field_type='array' (field '{self.name}')")
            if self.min_items > self.max_items:
                raise ValueError(f"min_items must be <= max_items (field '{self.name}')")
        return self

    @staticmethod
    def _validate_no_duplicate_names(fields: list["SchemaFieldConfig"], parent: str) -> None:
        seen: set[str] = set()
        for f in fields:
            if f.name in seen:
                raise ValueError(
                    f"Duplicate field name '{f.name}' in "
                    f"{'root schema' if not parent else f'embedded document {parent!r}'}"
                )
            seen.add(f.name)


class ReadTargetConfig(BaseModel):
    """Configuration for read operations."""
    filter_fields: list[str] = Field(default_factory=list, description="Fields to filter on for reads.")
    projection_fields: list[str] = Field(default_factory=list, description="Fields to include in read results (empty=all).")
    sort_field: str | None = Field(default=None, description="Field to sort by. Null=no sort.")
    sort_direction: Literal["asc", "desc"] = "desc"
    model_config = ConfigDict(extra="forbid")


class UpdateOperatorConfig(BaseModel):
    """Defines which MongoDB update operators to use."""
    set_fields: list[str] = Field(default_factory=list, description="Fields to $set with generated values.")
    inc_fields: list[str] = Field(default_factory=list, description="Fields to $inc by a random amount.")
    unset_fields: list[str] = Field(default_factory=list, description="Fields to $unset (remove).")
    push_fields: list[str] = Field(default_factory=list, description="Fields to $push a generated value onto (array fields).")
    current_date_fields: list[str] = Field(default_factory=list, description="Fields to set to current date via $currentDate.")
    model_config = ConfigDict(extra="forbid")


class UpdateTargetConfig(BaseModel):
    """Configuration for update operations."""
    filter_fields: list[str] = Field(default_factory=list, description="Fields to filter on for updates.")
    operators: UpdateOperatorConfig = Field(default_factory=UpdateOperatorConfig)
    model_config = ConfigDict(extra="forbid")


class DeleteTargetConfig(BaseModel):
    """Configuration for delete operations."""
    filter_fields: list[str] = Field(default_factory=list, description="Fields to filter on for deletes.")
    model_config = ConfigDict(extra="forbid")


class FindAndModifyTargetConfig(BaseModel):
    """Configuration for findAndModify operations."""
    filter_fields: list[str] = Field(default_factory=list, description="Fields to filter on.")
    operators: UpdateOperatorConfig = Field(default_factory=UpdateOperatorConfig)
    sort_field: str | None = Field(default=None, description="Field to sort by before modifying.")
    sort_direction: Literal["asc", "desc"] = "desc"
    return_updated: bool = Field(default=True, description="Return the modified document (true) or original (false).")
    upsert: bool = Field(default=False, description="Insert if no match found.")
    model_config = ConfigDict(extra="forbid")


class QueryTargetConfig(BaseModel):
    """Defines how each operation type finds and modifies documents."""

    enabled: bool = Field(
        default=False,
        description="When false, operations use _id-based lookups (default). "
        "When true, operations use the per-operation configurations below.",
    )
    read: ReadTargetConfig = Field(default_factory=ReadTargetConfig)
    update: UpdateTargetConfig = Field(default_factory=UpdateTargetConfig)
    delete: DeleteTargetConfig = Field(default_factory=DeleteTargetConfig)
    find_and_modify: FindAndModifyTargetConfig = Field(default_factory=FindAndModifyTargetConfig)
    create_indexes: bool = Field(
        default=True,
        description="Automatically create indexes on query target fields during seeding.",
    )

    model_config = ConfigDict(extra="forbid")

    @property
    def read_filter_fields(self) -> list[str]:
        return self.read.filter_fields

    @property
    def update_filter_fields(self) -> list[str]:
        return self.update.filter_fields

    @property
    def delete_filter_fields(self) -> list[str]:
        return self.delete.filter_fields


class SchemaConfig(BaseModel):
    """Definition for random payload generation."""

    fields: list[SchemaFieldConfig] = Field(
        default_factory=lambda: [
            SchemaFieldConfig(name="tenant_id", field_type="string", length=8),
            SchemaFieldConfig(name="order_total", field_type="float", min_value=10, max_value=5000),
            SchemaFieldConfig(name="status", field_type="choice", choices=["new", "paid", "shipped", "cancelled"]),
            SchemaFieldConfig(name="priority", field_type="int", min_value=1, max_value=5),
            SchemaFieldConfig(name="is_gift", field_type="bool"),
            SchemaFieldConfig(name="updated_at", field_type="datetime"),
        ]
    )
    query_targets: QueryTargetConfig = Field(default_factory=QueryTargetConfig)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_no_duplicate_field_names(self) -> "SchemaConfig":
        SchemaFieldConfig._validate_no_duplicate_names(self.fields, "")
        return self


class OperationMixConfig(BaseModel):
    """Operation percentages for OLTP-like traffic."""

    read: float = Field(default=0.2, ge=0)
    insert: float = Field(default=0.7, ge=0)
    update: float = Field(default=0.06, ge=0)
    delete: float = Field(default=0.02, ge=0)
    find_and_modify: float = Field(default=0.02, ge=0)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def ensure_positive_sum(self) -> "OperationMixConfig":
        total = self.read + self.insert + self.update + self.delete + self.find_and_modify
        if total <= 0:
            raise ValueError("Operation mix must have at least one positive weight")
        return self

    def normalized(self) -> dict[str, float]:
        total = self.read + self.insert + self.update + self.delete + self.find_and_modify
        return {
            "read": self.read / total,
            "insert": self.insert / total,
            "update": self.update / total,
            "delete": self.delete / total,
            "find_and_modify": self.find_and_modify / total,
        }


class UserLoadConfig(BaseModel):
    """Concurrency, pacing, and runtime controls."""

    virtual_users: int = Field(default=200, ge=1)
    worker_threads: int = Field(default=80, ge=1)
    max_worker_threads: int = Field(
        default=500, ge=1,
        description="Hard ceiling for worker threads. Requests above this are clamped.",
    )
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

    @model_validator(mode="after")
    def validate_worker_threads(self) -> "UserLoadConfig":
        if self.worker_threads > self.max_worker_threads:
            raise ValueError(
                f"worker_threads ({self.worker_threads}) must be <= "
                f"max_worker_threads ({self.max_worker_threads})"
            )
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


class LoggingConfig(BaseModel):
    """Application logging options."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    enable_file_output: bool = True
    log_file_path: str = "artifacts/mongo-load-tester.log"
    max_file_bytes: int = Field(default=20_000_000, ge=50_000)
    backup_count: int = Field(default=5, ge=1, le=50)
    ui_buffer_lines: int = Field(default=5_000, ge=200)
    ui_default_tail_lines: int = Field(default=250, ge=50, le=2_000)
    progress_log_interval_seconds: int = Field(default=5, ge=1, le=300)
    log_operation_failures: bool = True

    model_config = ConfigDict(extra="forbid")


class RetryConfig(BaseModel):
    """Retry controls for transient MongoDB errors."""

    max_attempts: int = Field(default=5, ge=1, le=50)
    initial_backoff_ms: int = Field(default=50, ge=1)
    max_backoff_ms: int = Field(default=2_000, ge=1)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    jitter_ratio: float = Field(default=0.2, ge=0.0, le=1.0)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_backoff_bounds(self) -> "RetryConfig":
        if self.max_backoff_ms < self.initial_backoff_ms:
            raise ValueError("max_backoff_ms must be >= initial_backoff_ms")
        return self


class MetadataConfig(BaseModel):
    """Metadata persistence and synchronization options."""

    database_name: str = "loader_metadata"
    load_test_id: str = "default-load-test"
    sync_interval_seconds: int = Field(default=30, ge=5, le=3600)

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("database_name", "load_test_id")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must be a non-empty string")
        return normalized


class AppConfig(BaseModel):
    """Root configuration object."""

    mongo: MongoConnectionConfig = Field(default_factory=MongoConnectionConfig)
    namespace: NamespaceConfig = Field(default_factory=NamespaceConfig)
    document_schema: SchemaConfig = Field(default_factory=SchemaConfig, alias="schema")
    operation_mix: OperationMixConfig = Field(default_factory=OperationMixConfig)
    load: UserLoadConfig = Field(default_factory=UserLoadConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    random_seed: int = Field(default=42, ge=0)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("random_seed")
    @classmethod
    def validate_seed(cls, value: int) -> int:
        if value >= 2**32:
            raise ValueError("random_seed must be < 2^32")
        return value


def load_config(config_path: str | Path) -> AppConfig:
    """Load and validate YAML config."""

    path = Path(config_path)
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            parsed = yaml.safe_load(file_obj)
    except FileNotFoundError as exc:
        raise ValueError(f"Configuration file not found: {path}") from exc
    except PermissionError as exc:
        raise ValueError(f"Permission denied reading configuration: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML syntax in {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Cannot read configuration file {path}: {exc}") from exc
    raw = parsed or {}
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        message = f"Invalid configuration at {path}: {exc}"
        raise ValueError(message) from exc


def default_config() -> AppConfig:
    """Build a default config object."""

    return AppConfig()

