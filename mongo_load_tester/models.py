"""Shared models for load execution and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OperationType(str, Enum):
    """Supported MongoDB operations."""

    READ = "read"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"

    @property
    def is_write(self) -> bool:
        return self in {OperationType.INSERT, OperationType.UPDATE, OperationType.DELETE}


@dataclass(slots=True)
class OperationResult:
    """Single operation execution outcome."""

    timestamp: datetime
    operation: OperationType
    database: str
    collection: str
    latency_ms: float
    success: bool
    error: str | None = None
    attempts: int = 1
    error_category: str | None = None
    timeout: bool = False


@dataclass(slots=True)
class TimelinePoint:
    """Aggregated metrics for a one-second time bucket."""

    second: datetime
    total_ops: int
    error_ops: int
    avg_latency_ms: float
    p95_latency_ms: float
    active_users: int = 0
    expected_users: int = 0
    user_delta: int = 0
    paused: bool = False
    timeout_ops: int = 0
    retried_ops: int = 0
    op_counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class MetricsSnapshot:
    """Thread-safe immutable view of current metrics."""

    generated_at: datetime
    elapsed_seconds: float
    total_ops: int
    success_ops: int
    error_ops: int
    ops_per_second: float
    avg_latency_ms: float
    p90_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    p999_latency_ms: float
    max_latency_ms: float
    latency_stddev_ms: float
    latency_jitter_ms: float
    active_virtual_users_current: int
    active_virtual_users_expected_current: int
    user_ramp_delta_current: int
    user_ramp_adherence_pct: float
    retried_ops: int
    retried_success_ops: int
    retry_success_rate_pct: float
    timeout_ops: int
    timeout_rate_pct: float
    total_attempts: int
    avg_attempts_per_op: float
    retry_attempt_distribution: dict[str, int]
    error_category_counts: dict[str, int]
    operation_counts: dict[str, int]
    operation_latency_avg_ms: dict[str, float]
    operation_latency_percentiles_ms: dict[str, dict[str, float]]
    rolling_ops_per_sec_10s: float
    rolling_ops_per_sec_60s: float
    throughput_cv_60s_pct: float
    slo_enabled: bool
    slo_status: str
    slo_within_pct: float
    slo_violation_seconds: int
    slo_failed_checks: list[str]
    timeline: list[TimelinePoint]
