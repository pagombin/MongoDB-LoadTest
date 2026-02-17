"""Thread-safe metrics aggregation for live dashboard and exports."""

from __future__ import annotations

import math
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock

from mongo_load_tester.models import MetricsSnapshot, OperationResult, TimelinePoint


@dataclass(slots=True)
class _TimelineBucket:
    total_ops: int = 0
    error_ops: int = 0
    latency_sum: float = 0.0
    latencies: list[float] = field(default_factory=list)
    op_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if percentile <= 0:
        return sorted_values[0]
    if percentile >= 100:
        return sorted_values[-1]
    rank = math.ceil((percentile / 100) * len(sorted_values)) - 1
    return sorted_values[max(rank, 0)]


class MetricsStore:
    """In-memory rolling metrics store safe for multi-threaded writes."""

    def __init__(self, history_window_seconds: int = 600, max_latency_samples: int = 500_000):
        self._history_window_seconds = history_window_seconds
        self._lock = Lock()
        self._start_wall_clock = time.time()
        self._start_monotonic = time.monotonic()
        self._total_ops = 0
        self._success_ops = 0
        self._error_ops = 0
        self._latency_sum = 0.0
        self._operation_counts: dict[str, int] = defaultdict(int)
        self._operation_latency_sum: dict[str, float] = defaultdict(float)
        self._latency_samples: deque[float] = deque(maxlen=max_latency_samples)
        self._timeline: OrderedDict[int, _TimelineBucket] = OrderedDict()

    def record(self, result: OperationResult) -> None:
        op_name = result.operation.value
        second_epoch = int(result.timestamp.timestamp())

        with self._lock:
            self._total_ops += 1
            self._latency_sum += result.latency_ms
            self._operation_counts[op_name] += 1
            self._operation_latency_sum[op_name] += result.latency_ms
            self._latency_samples.append(result.latency_ms)
            if result.success:
                self._success_ops += 1
            else:
                self._error_ops += 1

            bucket = self._timeline.setdefault(second_epoch, _TimelineBucket())
            bucket.total_ops += 1
            bucket.latency_sum += result.latency_ms
            bucket.latencies.append(result.latency_ms)
            bucket.op_counts[op_name] += 1
            if not result.success:
                bucket.error_ops += 1

            self._trim_timeline(second_epoch)

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            elapsed_seconds = max(time.monotonic() - self._start_monotonic, 1e-9)
            avg_latency = self._latency_sum / self._total_ops if self._total_ops else 0.0
            latency_values = sorted(self._latency_samples)
            p50 = _percentile(latency_values, 50)
            p95 = _percentile(latency_values, 95)
            p99 = _percentile(latency_values, 99)
            per_op_latency_avg = {
                op: self._operation_latency_sum[op] / count
                for op, count in self._operation_counts.items()
                if count
            }
            timeline = []
            for second_epoch, bucket in self._timeline.items():
                timeline_avg = bucket.latency_sum / bucket.total_ops if bucket.total_ops else 0.0
                timeline_p95 = _percentile(sorted(bucket.latencies), 95)
                timeline.append(
                    TimelinePoint(
                        second=datetime.fromtimestamp(second_epoch, tz=UTC),
                        total_ops=bucket.total_ops,
                        error_ops=bucket.error_ops,
                        avg_latency_ms=timeline_avg,
                        p95_latency_ms=timeline_p95,
                        op_counts=dict(bucket.op_counts),
                    )
                )

            return MetricsSnapshot(
                generated_at=datetime.now(UTC),
                elapsed_seconds=elapsed_seconds,
                total_ops=self._total_ops,
                success_ops=self._success_ops,
                error_ops=self._error_ops,
                ops_per_second=self._total_ops / elapsed_seconds,
                avg_latency_ms=avg_latency,
                p50_latency_ms=p50,
                p95_latency_ms=p95,
                p99_latency_ms=p99,
                operation_counts=dict(self._operation_counts),
                operation_latency_avg_ms=per_op_latency_avg,
                timeline=timeline,
            )

    def export_timeline_rows(self) -> list[dict[str, str | int | float]]:
        """Return per-second rows for CSV export."""

        with self._lock:
            rows: list[dict[str, str | int | float]] = []
            for second_epoch, bucket in self._timeline.items():
                rows.append(
                    {
                        "timestamp_utc": datetime.fromtimestamp(second_epoch, tz=UTC).isoformat(),
                        "total_ops": bucket.total_ops,
                        "error_ops": bucket.error_ops,
                        "avg_latency_ms": bucket.latency_sum / bucket.total_ops if bucket.total_ops else 0.0,
                        "p95_latency_ms": _percentile(sorted(bucket.latencies), 95),
                        "read_ops": bucket.op_counts.get("read", 0),
                        "insert_ops": bucket.op_counts.get("insert", 0),
                        "update_ops": bucket.op_counts.get("update", 0),
                        "delete_ops": bucket.op_counts.get("delete", 0),
                    }
                )
            return rows

    def _trim_timeline(self, current_second: int) -> None:
        cutoff = current_second - self._history_window_seconds
        while self._timeline:
            oldest_second = next(iter(self._timeline))
            if oldest_second >= cutoff:
                break
            self._timeline.popitem(last=False)
