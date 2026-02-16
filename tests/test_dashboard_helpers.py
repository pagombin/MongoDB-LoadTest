from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mongo_load_tester.dashboard.app import _filter_snapshot_timeline, _format_duration, _window_message
from mongo_load_tester.models import MetricsSnapshot, TimelinePoint


def _snapshot_with_points(now: datetime) -> MetricsSnapshot:
    timeline = [
        TimelinePoint(
            second=now - timedelta(seconds=120),
            total_ops=10,
            error_ops=0,
            avg_latency_ms=3.2,
            p95_latency_ms=5.1,
            op_counts={"read": 8, "insert": 2},
        ),
        TimelinePoint(
            second=now - timedelta(seconds=30),
            total_ops=11,
            error_ops=1,
            avg_latency_ms=4.1,
            p95_latency_ms=7.0,
            op_counts={"read": 7, "insert": 3, "update": 1},
        ),
    ]
    return MetricsSnapshot(
        generated_at=now,
        elapsed_seconds=130.0,
        total_ops=21,
        success_ops=20,
        error_ops=1,
        ops_per_second=0.16,
        avg_latency_ms=3.7,
        p50_latency_ms=3.0,
        p95_latency_ms=6.5,
        p99_latency_ms=7.0,
        operation_counts={"read": 15, "insert": 5, "update": 1},
        operation_latency_avg_ms={"read": 3.1, "insert": 4.5, "update": 5.2},
        timeline=timeline,
    )


def test_filter_snapshot_timeline_applies_window() -> None:
    now = datetime.now(UTC)
    snapshot = _snapshot_with_points(now)

    filtered = _filter_snapshot_timeline(snapshot, 60)

    assert len(filtered.timeline) == 1
    assert filtered.timeline[0].second == snapshot.timeline[1].second
    assert filtered.total_ops == snapshot.total_ops


def test_duration_and_window_labels_are_human_readable() -> None:
    assert _format_duration(45) == "45s"
    assert _format_duration(125) == "2m 5s"
    assert _format_duration(3600) == "1h"
    assert _window_message(0) == "Showing all captured points."
    assert _window_message(300) == "Showing last 5m of timeline data."
