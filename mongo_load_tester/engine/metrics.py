"""Thread-safe metrics aggregation for live dashboard and exports."""

from __future__ import annotations

import math
import random
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock

from mongo_load_tester.models import MetricsSnapshot, OperationResult, TimelinePoint

_RESERVOIR_SIZE = 10_000
_BUCKET_RESERVOIR_SIZE = 1_000


@dataclass(slots=True)
class _TimelineBucket:
    total_ops: int = 0
    error_ops: int = 0
    retried_ops: int = 0
    attempts_sum: int = 0
    latency_sum: float = 0.0
    latency_reservoir: list[float] = field(default_factory=list)
    latency_count: int = 0
    op_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add_latency(self, value: float) -> None:
        self.latency_count += 1
        if len(self.latency_reservoir) < _BUCKET_RESERVOIR_SIZE:
            self.latency_reservoir.append(value)
        else:
            idx = random.randrange(self.latency_count)
            if idx < _BUCKET_RESERVOIR_SIZE:
                self.latency_reservoir[idx] = value


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
        self._max_latency_samples = max_latency_samples
        self._lock = Lock()
        self._start_monotonic = time.monotonic()
        self._total_ops = 0
        self._success_ops = 0
        self._error_ops = 0
        self._total_attempts = 0
        self._retried_ops = 0
        self._latency_sum = 0.0
        self._operation_counts: dict[str, int] = defaultdict(int)
        self._operation_latency_sum: dict[str, float] = defaultdict(float)
        self._latency_reservoir: list[float] = []
        self._latency_total_count = 0
        self._timeline: OrderedDict[int, _TimelineBucket] = OrderedDict()
        self._active_user_timeline: OrderedDict[int, int] = OrderedDict()
        self._target_user_timeline: OrderedDict[int, int] = OrderedDict()
        self._paused_timeline: OrderedDict[int, bool] = OrderedDict()
        self._latest_active_users = 0
        self._latest_target_users = 0
        self._latest_paused_state = False

    def reset(self) -> None:
        """Clear collected metrics so a new run starts fresh."""

        with self._lock:
            self._start_monotonic = time.monotonic()
            self._total_ops = 0
            self._success_ops = 0
            self._error_ops = 0
            self._total_attempts = 0
            self._retried_ops = 0
            self._latency_sum = 0.0
            self._operation_counts.clear()
            self._operation_latency_sum.clear()
            self._latency_reservoir.clear()
            self._latency_total_count = 0
            self._timeline.clear()
            self._active_user_timeline.clear()
            self._target_user_timeline.clear()
            self._paused_timeline.clear()
            self._latest_active_users = 0
            self._latest_target_users = 0
            self._latest_paused_state = False

    def record(self, result: OperationResult) -> None:
        op_name = result.operation.value
        second_epoch = int(result.timestamp.timestamp())

        with self._lock:
            self._total_ops += 1
            self._latency_sum += result.latency_ms
            self._total_attempts += max(result.attempts, 1)
            if result.attempts > 1:
                self._retried_ops += 1
            self._operation_counts[op_name] += 1
            self._operation_latency_sum[op_name] += result.latency_ms
            self._add_latency_sample(result.latency_ms)
            if result.success:
                self._success_ops += 1
            else:
                self._error_ops += 1

            bucket = self._timeline.setdefault(second_epoch, _TimelineBucket())
            bucket.total_ops += 1
            bucket.latency_sum += result.latency_ms
            bucket.attempts_sum += max(result.attempts, 1)
            if result.attempts > 1:
                bucket.retried_ops += 1
            bucket.add_latency(result.latency_ms)
            bucket.op_counts[op_name] += 1
            if not result.success:
                bucket.error_ops += 1

            self._trim_history(second_epoch)

    def _add_latency_sample(self, value: float) -> None:
        """Reservoir sampling (Vitter's Algorithm R) for global latency percentiles."""
        self._latency_total_count += 1
        if len(self._latency_reservoir) < self._max_latency_samples:
            self._latency_reservoir.append(value)
        else:
            idx = random.randrange(self._latency_total_count)
            if idx < self._max_latency_samples:
                self._latency_reservoir[idx] = value

    def record_active_users(
        self,
        active_users: int,
        target_users: int | None = None,
        paused: bool = False,
        timestamp: datetime | None = None,
    ) -> None:
        """Record a sampled active-user count for the current second."""

        when = timestamp or datetime.now(UTC)
        second_epoch = int(when.timestamp())
        safe_value = max(active_users, 0)
        safe_target = max(target_users if target_users is not None else safe_value, 0)
        with self._lock:
            self._latest_active_users = safe_value
            self._latest_target_users = safe_target
            self._latest_paused_state = bool(paused)
            self._active_user_timeline[second_epoch] = safe_value
            self._target_user_timeline[second_epoch] = safe_target
            self._paused_timeline[second_epoch] = bool(paused)
            self._trim_history(second_epoch)

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            elapsed_seconds = max(time.monotonic() - self._start_monotonic, 1e-9)
            avg_latency = self._latency_sum / self._total_ops if self._total_ops else 0.0
            latency_values = sorted(self._latency_reservoir)
            p50 = _percentile(latency_values, 50)
            p95 = _percentile(latency_values, 95)
            p99 = _percentile(latency_values, 99)
            per_op_latency_avg = {
                op: self._operation_latency_sum[op] / count
                for op, count in self._operation_counts.items()
                if count
            }
            timeline = self._build_timeline_points()

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
                active_virtual_users_current=self._latest_active_users,
                retried_ops=self._retried_ops,
                total_attempts=self._total_attempts,
                avg_attempts_per_op=(self._total_attempts / self._total_ops) if self._total_ops else 0.0,
                operation_counts=dict(self._operation_counts),
                operation_latency_avg_ms=per_op_latency_avg,
                timeline=timeline,
            )

    def export_timeline_rows(self) -> list[dict[str, str | int | float | bool]]:
        """Return per-second rows for CSV export."""

        with self._lock:
            rows: list[dict[str, str | int | float | bool]] = []
            for second_epoch, bucket, active_users, target_users, paused in self._iterate_timeline():
                rows.append(
                    {
                        "timestamp_utc": datetime.fromtimestamp(second_epoch, tz=UTC).isoformat(),
                        "total_ops": bucket.total_ops,
                        "error_ops": bucket.error_ops,
                        "retried_ops": bucket.retried_ops,
                        "avg_attempts_per_op": bucket.attempts_sum / bucket.total_ops if bucket.total_ops else 0.0,
                        "avg_latency_ms": bucket.latency_sum / bucket.total_ops if bucket.total_ops else 0.0,
                        "p95_latency_ms": _percentile(sorted(bucket.latency_reservoir), 95),
                        "read_ops": bucket.op_counts.get("read", 0),
                        "insert_ops": bucket.op_counts.get("insert", 0),
                        "update_ops": bucket.op_counts.get("update", 0),
                        "delete_ops": bucket.op_counts.get("delete", 0),
                        "active_virtual_users": active_users,
                        "target_virtual_users": target_users,
                        "paused": paused,
                    }
                )
            return rows

    def _build_timeline_points(self) -> list[TimelinePoint]:
        """Shared timeline builder used by snapshot() — must be called under lock."""
        timeline: list[TimelinePoint] = []
        for second_epoch, bucket, active_users, target_users, paused in self._iterate_timeline():
            timeline_avg = bucket.latency_sum / bucket.total_ops if bucket.total_ops else 0.0
            timeline_p95 = _percentile(sorted(bucket.latency_reservoir), 95)
            timeline.append(
                TimelinePoint(
                    second=datetime.fromtimestamp(second_epoch, tz=UTC),
                    total_ops=bucket.total_ops,
                    error_ops=bucket.error_ops,
                    avg_latency_ms=timeline_avg,
                    p95_latency_ms=timeline_p95,
                    active_users=active_users,
                    target_users=target_users,
                    paused=paused,
                    op_counts=dict(bucket.op_counts),
                )
            )
        return timeline

    def _iterate_timeline(self):
        """Yield (second_epoch, bucket, active_users, target_users, paused) tuples.

        Must be called while holding self._lock.
        """
        all_seconds = sorted(
            set(self._timeline.keys())
            | set(self._active_user_timeline.keys())
            | set(self._target_user_timeline.keys())
            | set(self._paused_timeline.keys())
        )
        active_users = 0
        target_users = 0
        paused = False
        for second_epoch in all_seconds:
            if second_epoch in self._active_user_timeline:
                active_users = self._active_user_timeline[second_epoch]
            if second_epoch in self._target_user_timeline:
                target_users = self._target_user_timeline[second_epoch]
            if second_epoch in self._paused_timeline:
                paused = self._paused_timeline[second_epoch]
            bucket = self._timeline.get(second_epoch, _TimelineBucket())
            yield second_epoch, bucket, active_users, target_users, paused

    def _trim_history(self, current_second: int) -> None:
        cutoff = current_second - self._history_window_seconds
        for timeline_dict in (
            self._timeline,
            self._active_user_timeline,
            self._target_user_timeline,
            self._paused_timeline,
        ):
            while timeline_dict:
                oldest_second = next(iter(timeline_dict))
                if oldest_second >= cutoff:
                    break
                timeline_dict.popitem(last=False)
