from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mongo_load_tester.engine.metrics import MetricsStore, SLOPolicy
from mongo_load_tester.models import OperationResult, OperationType


def test_metrics_snapshot_calculations() -> None:
    store = MetricsStore(history_window_seconds=120)
    now = datetime.now(UTC)
    latencies = [1.0, 2.0, 3.0, 4.0]
    for idx, latency in enumerate(latencies):
        store.record(
            OperationResult(
                timestamp=now + timedelta(seconds=idx),
                operation=OperationType.READ if idx % 2 == 0 else OperationType.INSERT,
                database="db",
                collection="c",
                latency_ms=latency,
                success=idx != 3,
                error=None if idx != 3 else "boom",
                attempts=3 if idx == 2 else 1,
            )
        )
    store.record_active_users(25, paused=False, expected_users=25, timestamp=now + timedelta(seconds=4))
    store.record_active_users(0, paused=True, expected_users=25, timestamp=now + timedelta(seconds=5))

    snapshot = store.snapshot()
    assert snapshot.total_ops == 4
    assert snapshot.success_ops == 3
    assert snapshot.error_ops == 1
    assert snapshot.avg_latency_ms == 2.5
    assert snapshot.p50_latency_ms == 2.0
    assert snapshot.p95_latency_ms == 4.0
    assert snapshot.p99_latency_ms == 4.0
    assert snapshot.p999_latency_ms == 4.0
    assert snapshot.max_latency_ms == 4.0
    assert snapshot.p90_latency_ms == 4.0
    assert snapshot.timeout_ops == 0
    assert snapshot.retry_success_rate_pct == 100.0
    assert snapshot.retried_ops == 1
    assert snapshot.total_attempts == 6
    assert snapshot.avg_attempts_per_op == 1.5
    assert snapshot.active_virtual_users_current == 0
    assert snapshot.active_virtual_users_expected_current == 25
    assert snapshot.user_ramp_delta_current == -25
    assert snapshot.user_ramp_adherence_pct <= 100.0
    assert snapshot.rolling_ops_per_sec_10s >= 0
    assert snapshot.rolling_ops_per_sec_60s >= 0
    assert snapshot.slo_enabled is False
    assert snapshot.slo_status == "disabled"
    assert any(point.paused for point in snapshot.timeline)
    rows = store.export_timeline_rows()
    assert any(bool(row["paused"]) for row in rows)
    assert any("expected_virtual_users" in row for row in rows)


def test_metrics_store_reset_clears_all_data() -> None:
    store = MetricsStore(history_window_seconds=120)
    now = datetime.now(UTC)
    store.record(
        OperationResult(
            timestamp=now,
            operation=OperationType.READ,
            database="db",
            collection="c",
            latency_ms=2.0,
            success=True,
        )
    )
    store.record_active_users(10, paused=False, timestamp=now)

    store.reset()
    snapshot = store.snapshot()

    assert snapshot.total_ops == 0
    assert snapshot.error_ops == 0
    assert snapshot.retried_ops == 0
    assert snapshot.active_virtual_users_current == 0
    assert snapshot.timeline == []
    assert store.export_timeline_rows() == []


def test_slo_evaluation_reports_failures() -> None:
    policy = SLOPolicy(
        enabled=True,
        p95_latency_ms_threshold=3.0,
        error_rate_pct_threshold=0.0,
        min_ops_per_sec_threshold=1.0,
        evaluation_window_seconds=120,
    )
    store = MetricsStore(history_window_seconds=300, slo_policy=policy)
    now = datetime.now(UTC)
    store.record(
        OperationResult(
            timestamp=now,
            operation=OperationType.READ,
            database="db",
            collection="c",
            latency_ms=5.0,
            success=True,
        )
    )
    store.record(
        OperationResult(
            timestamp=now,
            operation=OperationType.READ,
            database="db",
            collection="c",
            latency_ms=6.0,
            success=True,
        )
    )

    snapshot = store.snapshot()
    assert snapshot.slo_enabled is True
    assert snapshot.slo_status == "fail"
    assert "p95_latency" in snapshot.slo_failed_checks
