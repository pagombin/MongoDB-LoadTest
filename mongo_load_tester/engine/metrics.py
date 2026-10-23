"""Thread-safe metrics aggregation for live dashboard and exports."""

from __future__ import annotations

import math
import random
import re
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock

from mongo_load_tester.models import MetricsSnapshot, OperationResult, TimelinePoint

_RESERVOIR_SIZE = 10_000
_BUCKET_RESERVOIR_SIZE = 1_000
_PER_OP_RESERVOIR_SIZE = 5_000

_HISTOGRAM_BUCKET_BOUNDARIES = [
    0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0,
    200.0, 500.0, 1000.0, 2000.0, 5000.0, 10_000.0,
]


def _categorize_error(error_str: str | None) -> str:
    """Classify a MongoDB error string into a human-readable category."""
    if not error_str:
        return "unknown"
    lower = error_str.lower()
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    if "connection" in lower or "socket" in lower or "network" in lower:
        return "network"
    if "not primary" in lower or "not master" in lower or "stepdown" in lower:
        return "primary_stepdown"
    if "server selection" in lower:
        return "server_selection"
    if "duplicate key" in lower:
        return "duplicate_key"
    if "cursor not found" in lower or "cursor id" in lower:
        return "cursor"
    if "write concern" in lower:
        return "write_concern"
    if re.search(r"\bauthentication failed\b|\bunauthorized\b|\bnot authorized\b|\bauth failed\b", lower):
        return "auth"
    return "other"


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
    op_latency_sums: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    op_latency_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

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


def _build_histogram(sorted_values: list[float]) -> list[tuple[float, int]]:
    """Build a histogram from sorted latency values using fixed bucket boundaries."""
    if not sorted_values:
        return []
    result: list[tuple[float, int]] = []
    val_idx = 0
    n = len(sorted_values)
    prev_boundary = 0.0
    for boundary in _HISTOGRAM_BUCKET_BOUNDARIES:
        count = 0
        while val_idx < n and sorted_values[val_idx] <= boundary:
            count += 1
            val_idx += 1
        if count > 0 or result:
            result.append((boundary, count))
        prev_boundary = boundary
    remaining = n - val_idx
    if remaining > 0:
        result.append((sorted_values[-1], remaining))
    return result


class _PerOpReservoir:
    """Reservoir sampler for per-operation latency tracking."""

    __slots__ = ("_samples", "_count", "_max_size")

    def __init__(self, max_size: int = _PER_OP_RESERVOIR_SIZE):
        self._samples: list[float] = []
        self._count = 0
        self._max_size = max_size

    def add(self, value: float) -> None:
        self._count += 1
        if len(self._samples) < self._max_size:
            self._samples.append(value)
        else:
            idx = random.randrange(self._count)
            if idx < self._max_size:
                self._samples[idx] = value

    def percentiles(self) -> tuple[float, float, float]:
        """Return (p50, p95, p99) from current samples."""
        if not self._samples:
            return 0.0, 0.0, 0.0
        s = sorted(self._samples)
        return _percentile(s, 50), _percentile(s, 95), _percentile(s, 99)

    def clear(self) -> None:
        self._samples.clear()
        self._count = 0


class MetricsStore:
    """In-memory rolling metrics store safe for multi-threaded writes."""

    def __init__(self, history_window_seconds: int = 600, max_latency_samples: int = 10_000):
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
        self._operation_latency_reservoirs: dict[str, _PerOpReservoir] = defaultdict(_PerOpReservoir)
        self._latency_reservoir: list[float] = []
        self._latency_total_count = 0
        self._error_categories: dict[str, int] = defaultdict(int)
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
            for r in self._operation_latency_reservoirs.values():
                r.clear()
            self._operation_latency_reservoirs.clear()
            self._latency_reservoir.clear()
            self._latency_total_count = 0
            self._error_categories.clear()
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
            self._operation_latency_reservoirs[op_name].add(result.latency_ms)
            self._add_latency_sample(result.latency_ms)
            if result.success:
                self._success_ops += 1
            else:
                self._error_ops += 1
                category = _categorize_error(result.error)
                self._error_categories[category] += 1

            bucket = self._timeline.setdefault(second_epoch, _TimelineBucket())
            bucket.total_ops += 1
            bucket.latency_sum += result.latency_ms
            bucket.attempts_sum += max(result.attempts, 1)
            if result.attempts > 1:
                bucket.retried_ops += 1
            bucket.add_latency(result.latency_ms)
            bucket.op_counts[op_name] += 1
            bucket.op_latency_sums[op_name] += result.latency_ms
            bucket.op_latency_counts[op_name] += 1
            if not result.success:
                bucket.error_ops += 1

            self._trim_history(second_epoch)

    def record_batch(self, results: list[OperationResult]) -> None:
        """Record multiple results in a single lock acquisition."""
        if not results:
            return
        with self._lock:
            for result in results:
                op_name = result.operation.value
                second_epoch = int(result.timestamp.timestamp())
                self._total_ops += 1
                self._latency_sum += result.latency_ms
                self._total_attempts += max(result.attempts, 1)
                if result.attempts > 1:
                    self._retried_ops += 1
                self._operation_counts[op_name] += 1
                self._operation_latency_sum[op_name] += result.latency_ms
                self._operation_latency_reservoirs[op_name].add(result.latency_ms)
                self._add_latency_sample(result.latency_ms)
                if result.success:
                    self._success_ops += 1
                else:
                    self._error_ops += 1
                    category = _categorize_error(result.error)
                    self._error_categories[category] += 1
                bucket = self._timeline.setdefault(second_epoch, _TimelineBucket())
                bucket.total_ops += 1
                bucket.latency_sum += result.latency_ms
                bucket.attempts_sum += max(result.attempts, 1)
                if result.attempts > 1:
                    bucket.retried_ops += 1
                bucket.add_latency(result.latency_ms)
                bucket.op_counts[op_name] += 1
                bucket.op_latency_sums[op_name] += result.latency_ms
                bucket.op_latency_counts[op_name] += 1
                if not result.success:
                    bucket.error_ops += 1
            self._trim_history(int(results[-1].timestamp.timestamp()))

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
            per_op_p95: dict[str, float] = {}
            per_op_p99: dict[str, float] = {}
            for op, reservoir in self._operation_latency_reservoirs.items():
                _, op_p95, op_p99 = reservoir.percentiles()
                per_op_p95[op] = op_p95
                per_op_p99[op] = op_p99

            histogram = _build_histogram(latency_values)
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
                operation_latency_p95_ms=per_op_p95,
                operation_latency_p99_ms=per_op_p99,
                latency_histogram=histogram,
                error_categories=dict(self._error_categories),
            )

    def export_timeline_rows(self) -> list[dict[str, str | int | float | bool]]:
        """Return per-second rows for CSV export."""

        with self._lock:
            rows: list[dict[str, str | int | float | bool]] = []
            for second_epoch, bucket, active_users, target_users, paused in self._iterate_timeline():
                sorted_res = sorted(bucket.latency_reservoir)
                rows.append(
                    {
                        "timestamp_utc": datetime.fromtimestamp(second_epoch, tz=UTC).isoformat(),
                        "total_ops": bucket.total_ops,
                        "error_ops": bucket.error_ops,
                        "retried_ops": bucket.retried_ops,
                        "avg_attempts_per_op": bucket.attempts_sum / bucket.total_ops if bucket.total_ops else 0.0,
                        "avg_latency_ms": bucket.latency_sum / bucket.total_ops if bucket.total_ops else 0.0,
                        "p50_latency_ms": _percentile(sorted_res, 50),
                        "p95_latency_ms": _percentile(sorted_res, 95),
                        "p99_latency_ms": _percentile(sorted_res, 99),
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
            sorted_res = sorted(bucket.latency_reservoir)
            timeline_p50 = _percentile(sorted_res, 50)
            timeline_p95 = _percentile(sorted_res, 95)
            timeline_p99 = _percentile(sorted_res, 99)
            op_latency_avg: dict[str, float] = {}
            for op, total_lat in bucket.op_latency_sums.items():
                count = bucket.op_latency_counts.get(op, 0)
                if count > 0:
                    op_latency_avg[op] = total_lat / count
            timeline.append(
                TimelinePoint(
                    second=datetime.fromtimestamp(second_epoch, tz=UTC),
                    total_ops=bucket.total_ops,
                    error_ops=bucket.error_ops,
                    avg_latency_ms=timeline_avg,
                    p50_latency_ms=timeline_p50,
                    p95_latency_ms=timeline_p95,
                    p99_latency_ms=timeline_p99,
                    retried_ops=bucket.retried_ops,
                    active_users=active_users,
                    target_users=target_users,
                    paused=paused,
                    op_counts=dict(bucket.op_counts),
                    op_latency_avg=op_latency_avg,
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
