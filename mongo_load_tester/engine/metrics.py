"""Thread-safe metrics aggregation for live dashboard and exports."""

from __future__ import annotations

import math
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock

from mongo_load_tester.models import MetricsSnapshot, OperationResult, TimelinePoint


@dataclass(frozen=True, slots=True)
class SLOPolicy:
    """SLO thresholds used for client-side compliance calculation."""

    enabled: bool = True
    p95_latency_ms_threshold: float = 80.0
    error_rate_pct_threshold: float = 1.0
    min_ops_per_sec_threshold: float = 100.0
    evaluation_window_seconds: int = 300

    @classmethod
    def disabled(cls) -> "SLOPolicy":
        return cls(enabled=False)


@dataclass(slots=True)
class _TimelineBucket:
    total_ops: int = 0
    error_ops: int = 0
    retried_ops: int = 0
    timeout_ops: int = 0
    attempts_sum: int = 0
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


def _rolling_average(values: list[int], window: int) -> float:
    if not values:
        return 0.0
    sample = values[-window:] if len(values) > window else values
    return sum(sample) / len(sample)


def _coefficient_variation_pct(values: list[int]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return (math.sqrt(variance) / mean) * 100.0


class MetricsStore:
    """In-memory rolling metrics store safe for multi-threaded writes."""

    def __init__(
        self,
        history_window_seconds: int = 600,
        max_latency_samples: int = 500_000,
        slo_policy: SLOPolicy | None = None,
    ):
        self._history_window_seconds = history_window_seconds
        self._lock = Lock()
        self._slo_policy = slo_policy or SLOPolicy.disabled()
        self._max_latency_samples = max_latency_samples

        self._start_monotonic = time.monotonic()
        self._total_ops = 0
        self._success_ops = 0
        self._error_ops = 0
        self._total_attempts = 0
        self._retried_ops = 0
        self._retried_success_ops = 0
        self._timeout_ops = 0

        self._latency_sum = 0.0
        self._latency_squared_sum = 0.0
        self._latency_abs_diff_sum = 0.0
        self._latency_diff_count = 0
        self._last_latency_ms: float | None = None

        self._operation_counts: dict[str, int] = defaultdict(int)
        self._operation_latency_sum: dict[str, float] = defaultdict(float)
        self._operation_latency_samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=max(20_000, self._max_latency_samples // 4))
        )
        self._error_category_counts: dict[str, int] = defaultdict(int)
        self._retry_attempt_distribution: dict[str, int] = defaultdict(int)
        self._latency_samples: deque[float] = deque(maxlen=max_latency_samples)

        self._timeline: OrderedDict[int, _TimelineBucket] = OrderedDict()
        self._active_user_timeline: OrderedDict[int, int] = OrderedDict()
        self._expected_user_timeline: OrderedDict[int, int] = OrderedDict()
        self._paused_timeline: OrderedDict[int, bool] = OrderedDict()
        self._latest_active_users = 0
        self._latest_expected_users = 0

    def reset(self) -> None:
        """Clear collected metrics so a new run starts fresh."""

        with self._lock:
            self._start_monotonic = time.monotonic()
            self._total_ops = 0
            self._success_ops = 0
            self._error_ops = 0
            self._total_attempts = 0
            self._retried_ops = 0
            self._retried_success_ops = 0
            self._timeout_ops = 0
            self._latency_sum = 0.0
            self._latency_squared_sum = 0.0
            self._latency_abs_diff_sum = 0.0
            self._latency_diff_count = 0
            self._last_latency_ms = None

            self._operation_counts.clear()
            self._operation_latency_sum.clear()
            self._operation_latency_samples.clear()
            self._error_category_counts.clear()
            self._retry_attempt_distribution.clear()
            self._latency_samples.clear()
            self._timeline.clear()
            self._active_user_timeline.clear()
            self._expected_user_timeline.clear()
            self._paused_timeline.clear()
            self._latest_active_users = 0
            self._latest_expected_users = 0

    def record(self, result: OperationResult) -> None:
        op_name = result.operation.value
        second_epoch = int(result.timestamp.timestamp())
        attempts = max(result.attempts, 1)
        attempt_bucket = str(attempts) if attempts <= 3 else "4+"

        with self._lock:
            self._total_ops += 1
            self._latency_sum += result.latency_ms
            self._latency_squared_sum += result.latency_ms**2
            if self._last_latency_ms is not None:
                self._latency_abs_diff_sum += abs(result.latency_ms - self._last_latency_ms)
                self._latency_diff_count += 1
            self._last_latency_ms = result.latency_ms

            self._total_attempts += attempts
            if attempts > 1:
                self._retried_ops += 1
                if result.success:
                    self._retried_success_ops += 1
            self._retry_attempt_distribution[attempt_bucket] += 1
            if result.timeout:
                self._timeout_ops += 1
            if result.error_category:
                self._error_category_counts[result.error_category] += 1

            self._operation_counts[op_name] += 1
            self._operation_latency_sum[op_name] += result.latency_ms
            self._operation_latency_samples[op_name].append(result.latency_ms)
            self._latency_samples.append(result.latency_ms)
            if result.success:
                self._success_ops += 1
            else:
                self._error_ops += 1

            bucket = self._timeline.setdefault(second_epoch, _TimelineBucket())
            bucket.total_ops += 1
            bucket.latency_sum += result.latency_ms
            bucket.attempts_sum += attempts
            if attempts > 1:
                bucket.retried_ops += 1
            if result.timeout:
                bucket.timeout_ops += 1
            bucket.latencies.append(result.latency_ms)
            bucket.op_counts[op_name] += 1
            if not result.success:
                bucket.error_ops += 1

            self._trim_history(second_epoch)

    def record_active_users(
        self,
        active_users: int,
        paused: bool = False,
        expected_users: int | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        """Record sampled active/expected-user counts for the current second."""

        when = timestamp or datetime.now(UTC)
        second_epoch = int(when.timestamp())
        safe_active = max(active_users, 0)
        safe_expected = max(expected_users if expected_users is not None else safe_active, 0)
        with self._lock:
            self._latest_active_users = safe_active
            self._latest_expected_users = safe_expected
            self._active_user_timeline[second_epoch] = safe_active
            self._expected_user_timeline[second_epoch] = safe_expected
            self._paused_timeline[second_epoch] = bool(paused)
            self._trim_history(second_epoch)

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            elapsed_seconds = max(time.monotonic() - self._start_monotonic, 1e-9)
            avg_latency = self._latency_sum / self._total_ops if self._total_ops else 0.0
            latency_values = sorted(self._latency_samples)

            p50 = _percentile(latency_values, 50)
            p90 = _percentile(latency_values, 90)
            p95 = _percentile(latency_values, 95)
            p99 = _percentile(latency_values, 99)
            p999 = _percentile(latency_values, 99.9)
            max_latency = latency_values[-1] if latency_values else 0.0
            variance = (self._latency_squared_sum / self._total_ops) - (avg_latency**2) if self._total_ops else 0.0
            latency_stddev = math.sqrt(max(variance, 0.0))
            latency_jitter = (
                self._latency_abs_diff_sum / self._latency_diff_count if self._latency_diff_count else 0.0
            )

            per_op_latency_avg = {
                op: self._operation_latency_sum[op] / count
                for op, count in self._operation_counts.items()
                if count
            }
            per_op_latency_percentiles: dict[str, dict[str, float]] = {}
            for op_name, samples in self._operation_latency_samples.items():
                if not samples:
                    continue
                sorted_samples = sorted(samples)
                per_op_latency_percentiles[op_name] = {
                    "p95": _percentile(sorted_samples, 95),
                    "p99": _percentile(sorted_samples, 99),
                }

            timeline = []
            all_seconds = sorted(
                set(self._timeline.keys())
                | set(self._active_user_timeline.keys())
                | set(self._expected_user_timeline.keys())
                | set(self._paused_timeline.keys())
            )
            active_users = 0
            expected_users = 0
            paused = False
            for second_epoch in all_seconds:
                if second_epoch in self._active_user_timeline:
                    active_users = self._active_user_timeline[second_epoch]
                if second_epoch in self._expected_user_timeline:
                    expected_users = self._expected_user_timeline[second_epoch]
                if second_epoch in self._paused_timeline:
                    paused = self._paused_timeline[second_epoch]
                bucket = self._timeline.get(second_epoch, _TimelineBucket())
                timeline_avg = bucket.latency_sum / bucket.total_ops if bucket.total_ops else 0.0
                timeline_p95 = _percentile(sorted(bucket.latencies), 95)
                timeline.append(
                    TimelinePoint(
                        second=datetime.fromtimestamp(second_epoch, tz=UTC),
                        total_ops=bucket.total_ops,
                        error_ops=bucket.error_ops,
                        avg_latency_ms=timeline_avg,
                        p95_latency_ms=timeline_p95,
                        active_users=active_users,
                        expected_users=expected_users,
                        user_delta=active_users - expected_users,
                        paused=paused,
                        timeout_ops=bucket.timeout_ops,
                        retried_ops=bucket.retried_ops,
                        op_counts=dict(bucket.op_counts),
                    )
                )

            ops_series = [point.total_ops for point in timeline]
            rolling_10 = _rolling_average(ops_series, 10)
            rolling_60 = _rolling_average(ops_series, 60)
            throughput_cv_60 = _coefficient_variation_pct(ops_series[-60:] if len(ops_series) > 60 else ops_series)
            ramp_adherence = self._user_ramp_adherence_pct(timeline)
            (
                slo_status,
                slo_within_pct,
                slo_violation_seconds,
                slo_failed_checks,
            ) = self._evaluate_slo(timeline)

            retry_success_rate = (self._retried_success_ops / self._retried_ops * 100.0) if self._retried_ops else 0.0
            timeout_rate = (self._timeout_ops / self._total_ops * 100.0) if self._total_ops else 0.0

            return MetricsSnapshot(
                generated_at=datetime.now(UTC),
                elapsed_seconds=elapsed_seconds,
                total_ops=self._total_ops,
                success_ops=self._success_ops,
                error_ops=self._error_ops,
                ops_per_second=self._total_ops / elapsed_seconds,
                avg_latency_ms=avg_latency,
                p90_latency_ms=p90,
                p50_latency_ms=p50,
                p95_latency_ms=p95,
                p99_latency_ms=p99,
                p999_latency_ms=p999,
                max_latency_ms=max_latency,
                latency_stddev_ms=latency_stddev,
                latency_jitter_ms=latency_jitter,
                active_virtual_users_current=self._latest_active_users,
                active_virtual_users_expected_current=self._latest_expected_users,
                user_ramp_delta_current=self._latest_active_users - self._latest_expected_users,
                user_ramp_adherence_pct=ramp_adherence,
                retried_ops=self._retried_ops,
                retried_success_ops=self._retried_success_ops,
                retry_success_rate_pct=retry_success_rate,
                timeout_ops=self._timeout_ops,
                timeout_rate_pct=timeout_rate,
                total_attempts=self._total_attempts,
                avg_attempts_per_op=(self._total_attempts / self._total_ops) if self._total_ops else 0.0,
                retry_attempt_distribution=dict(self._retry_attempt_distribution),
                error_category_counts=dict(self._error_category_counts),
                operation_counts=dict(self._operation_counts),
                operation_latency_avg_ms=per_op_latency_avg,
                operation_latency_percentiles_ms=per_op_latency_percentiles,
                rolling_ops_per_sec_10s=rolling_10,
                rolling_ops_per_sec_60s=rolling_60,
                throughput_cv_60s_pct=throughput_cv_60,
                slo_enabled=self._slo_policy.enabled,
                slo_status=slo_status,
                slo_within_pct=slo_within_pct,
                slo_violation_seconds=slo_violation_seconds,
                slo_failed_checks=slo_failed_checks,
                timeline=timeline,
            )

    def export_timeline_rows(self) -> list[dict[str, str | int | float | bool]]:
        """Return per-second rows for CSV export."""

        with self._lock:
            rows: list[dict[str, str | int | float | bool]] = []
            all_seconds = sorted(
                set(self._timeline.keys())
                | set(self._active_user_timeline.keys())
                | set(self._expected_user_timeline.keys())
                | set(self._paused_timeline.keys())
            )
            active_users = 0
            expected_users = 0
            paused = False
            ops_history: list[int] = []

            for second_epoch in all_seconds:
                if second_epoch in self._active_user_timeline:
                    active_users = self._active_user_timeline[second_epoch]
                if second_epoch in self._expected_user_timeline:
                    expected_users = self._expected_user_timeline[second_epoch]
                if second_epoch in self._paused_timeline:
                    paused = self._paused_timeline[second_epoch]
                bucket = self._timeline.get(second_epoch, _TimelineBucket())
                ops_history.append(bucket.total_ops)
                row = {
                    "timestamp_utc": datetime.fromtimestamp(second_epoch, tz=UTC).isoformat(),
                    "total_ops": bucket.total_ops,
                    "error_ops": bucket.error_ops,
                    "timeout_ops": bucket.timeout_ops,
                    "retried_ops": bucket.retried_ops,
                    "avg_attempts_per_op": bucket.attempts_sum / bucket.total_ops if bucket.total_ops else 0.0,
                    "avg_latency_ms": bucket.latency_sum / bucket.total_ops if bucket.total_ops else 0.0,
                    "p95_latency_ms": _percentile(sorted(bucket.latencies), 95),
                    "read_ops": bucket.op_counts.get("read", 0),
                    "insert_ops": bucket.op_counts.get("insert", 0),
                    "update_ops": bucket.op_counts.get("update", 0),
                    "delete_ops": bucket.op_counts.get("delete", 0),
                    "active_virtual_users": active_users,
                    "expected_virtual_users": expected_users,
                    "user_delta": active_users - expected_users,
                    "paused": paused,
                    "ops_per_sec_rolling_10s": _rolling_average(ops_history, 10),
                    "ops_per_sec_rolling_60s": _rolling_average(ops_history, 60),
                }
                rows.append(row)
            return rows

    def _trim_history(self, current_second: int) -> None:
        cutoff = current_second - self._history_window_seconds
        while self._timeline:
            oldest_second = next(iter(self._timeline))
            if oldest_second >= cutoff:
                break
            self._timeline.popitem(last=False)
        while self._active_user_timeline:
            oldest_second = next(iter(self._active_user_timeline))
            if oldest_second >= cutoff:
                break
            self._active_user_timeline.popitem(last=False)
        while self._expected_user_timeline:
            oldest_second = next(iter(self._expected_user_timeline))
            if oldest_second >= cutoff:
                break
            self._expected_user_timeline.popitem(last=False)
        while self._paused_timeline:
            oldest_second = next(iter(self._paused_timeline))
            if oldest_second >= cutoff:
                break
            self._paused_timeline.popitem(last=False)

    def _user_ramp_adherence_pct(self, timeline: list[TimelinePoint]) -> float:
        ratios: list[float] = []
        for point in timeline:
            if point.expected_users <= 0:
                continue
            ratio = min(max(point.active_users, 0) / point.expected_users, 1.0)
            ratios.append(ratio)
        if not ratios:
            return 100.0
        return (sum(ratios) / len(ratios)) * 100.0

    def _evaluate_slo(self, timeline: list[TimelinePoint]) -> tuple[str, float, int, list[str]]:
        if not self._slo_policy.enabled:
            return "disabled", 100.0, 0, []
        if not timeline:
            return "no_data", 0.0, 0, ["no_data"]

        latest_second = timeline[-1].second
        cutoff = latest_second - timedelta(seconds=self._slo_policy.evaluation_window_seconds)
        window_points = [point for point in timeline if point.second >= cutoff and not point.paused]
        if not window_points:
            return "no_data", 0.0, 0, ["no_data"]

        ok_count = 0
        for point in window_points:
            error_rate_pct = (point.error_ops / point.total_ops * 100.0) if point.total_ops else 0.0
            point_checks = (
                point.p95_latency_ms <= self._slo_policy.p95_latency_ms_threshold,
                error_rate_pct <= self._slo_policy.error_rate_pct_threshold,
                point.total_ops >= self._slo_policy.min_ops_per_sec_threshold,
            )
            if all(point_checks):
                ok_count += 1

        latest = window_points[-1]
        latest_error_rate = (latest.error_ops / latest.total_ops * 100.0) if latest.total_ops else 0.0
        failed_checks: list[str] = []
        if latest.p95_latency_ms > self._slo_policy.p95_latency_ms_threshold:
            failed_checks.append("p95_latency")
        if latest_error_rate > self._slo_policy.error_rate_pct_threshold:
            failed_checks.append("error_rate")
        if latest.total_ops < self._slo_policy.min_ops_per_sec_threshold:
            failed_checks.append("ops_per_sec")

        within_pct = (ok_count / len(window_points)) * 100.0
        violation_seconds = len(window_points) - ok_count
        status = "pass" if not failed_checks else "fail"
        return status, within_pct, violation_seconds, failed_checks
