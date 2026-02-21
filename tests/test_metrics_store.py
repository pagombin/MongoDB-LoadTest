from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mongo_load_tester.engine.metrics import MetricsStore
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
    store.record_active_users(25, paused=False, timestamp=now + timedelta(seconds=4))
    store.record_active_users(0, paused=True, timestamp=now + timedelta(seconds=5))

    snapshot = store.snapshot()
    assert snapshot.total_ops == 4
    assert snapshot.success_ops == 3
    assert snapshot.error_ops == 1
    assert snapshot.avg_latency_ms == 2.5
    assert snapshot.p50_latency_ms == 2.0
    assert snapshot.p95_latency_ms == 4.0
    assert snapshot.p99_latency_ms == 4.0
    assert snapshot.retried_ops == 1
    assert snapshot.total_attempts == 6
    assert snapshot.avg_attempts_per_op == 1.5
    assert snapshot.active_virtual_users_current == 0
    assert any(point.paused for point in snapshot.timeline)
    rows = store.export_timeline_rows()
    assert any(bool(row["paused"]) for row in rows)


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
