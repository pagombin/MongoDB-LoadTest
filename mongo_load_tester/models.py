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
    FIND_AND_MODIFY = "find_and_modify"

    @property
    def is_write(self) -> bool:
        return self in {OperationType.INSERT, OperationType.UPDATE, OperationType.DELETE, OperationType.FIND_AND_MODIFY}


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


@dataclass(slots=True)
class TimelinePoint:
    """Aggregated metrics for a one-second time bucket."""

    second: datetime
    total_ops: int
    error_ops: int
    avg_latency_ms: float
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    retried_ops: int = 0
    active_users: int = 0
    target_users: int = 0
    paused: bool = False
    op_counts: dict[str, int] = field(default_factory=dict)
    op_latency_avg: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """Thread-safe immutable view of current metrics."""

    generated_at: datetime
    elapsed_seconds: float
    total_ops: int
    success_ops: int
    error_ops: int
    ops_per_second: float
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    active_virtual_users_current: int
    retried_ops: int
    total_attempts: int
    avg_attempts_per_op: float
    operation_counts: dict[str, int]
    operation_latency_avg_ms: dict[str, float]
    timeline: list[TimelinePoint]
    operation_latency_p95_ms: dict[str, float] = field(default_factory=dict)
    operation_latency_p99_ms: dict[str, float] = field(default_factory=dict)
    latency_histogram: list[tuple[float, int]] = field(default_factory=list)
    error_categories: dict[str, int] = field(default_factory=dict)
