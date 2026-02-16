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


@dataclass(slots=True)
class TimelinePoint:
    """Aggregated metrics for a one-second time bucket."""

    second: datetime
    total_ops: int
    error_ops: int
    avg_latency_ms: float
    p95_latency_ms: float
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
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    operation_counts: dict[str, int]
    operation_latency_avg_ms: dict[str, float]
    timeline: list[TimelinePoint]
