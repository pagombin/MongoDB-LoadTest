from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mongo_load_tester.dashboard.app import (
    _compute_stable_ops_per_sec,
    _filter_snapshot_timeline,
    _format_datetime_short,
    _format_duration,
    _parse_non_negative_int,
    _parse_schema_config,
    _remove_incomplete_live_second,
    _schema_config_to_json,
    _snapshot_from_persisted_rows,
    _static_config_diff_labels,
    _static_config_snapshot,
    _validate_think_time_values,
    _window_message,
)
from mongo_load_tester.config import AppConfig
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
        active_virtual_users_current=120,
        retried_ops=2,
        total_attempts=23,
        avg_attempts_per_op=1.0952,
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
    now = datetime.now(UTC)
    snapshot = _snapshot_with_points(now)
    assert _format_duration(45) == "45s"
    assert _format_duration(125) == "2m 5s"
    assert _format_duration(3600) == "1h"
    assert _window_message(0, snapshot) == "Showing all captured points."
    assert _window_message(300, snapshot).startswith("Showing last 5m window")


def test_remove_incomplete_live_second_filters_latest_partial_point() -> None:
    now = datetime.now(UTC).replace(microsecond=500_000)
    snapshot = _snapshot_with_points(now)
    snapshot.timeline.append(
        TimelinePoint(
            second=now.replace(microsecond=0),
            total_ops=2,
            error_ops=0,
            avg_latency_ms=1.0,
            p95_latency_ms=1.5,
            op_counts={"read": 2},
        )
    )

    filtered = _remove_incomplete_live_second(snapshot, is_running=True)

    assert all(point.second < now.replace(microsecond=0) for point in filtered.timeline)


def test_compute_stable_ops_per_sec_uses_active_elapsed() -> None:
    assert _compute_stable_ops_per_sec(0, 5.0) == 0.0
    assert _compute_stable_ops_per_sec(100, 0.0) == 0.0
    assert _compute_stable_ops_per_sec(100, 4.0) == 25.0


def test_validate_think_time_values_accepts_valid_range() -> None:
    assert _validate_think_time_values(0, 25) == (0, 25)


def test_validate_think_time_values_rejects_invalid_range() -> None:
    try:
        _validate_think_time_values(30, 10)
    except ValueError as exc:
        assert "minimum cannot be greater" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid think time range")


def test_schema_json_roundtrip_and_validation() -> None:
    schema = _parse_schema_config(
        '[{"name":"tenant_id","field_type":"string","length":8},{"name":"amount","field_type":"float"}]'
    )
    rendered = _schema_config_to_json(schema)
    parsed_again = _parse_schema_config(rendered)
    assert len(parsed_again.fields) == 2
    assert parsed_again.fields[0].name == "tenant_id"


def test_parse_non_negative_int_rejects_negative_values() -> None:
    try:
        _parse_non_negative_int(-1, "Initial seed")
    except ValueError as exc:
        assert "zero or greater" in str(exc)
    else:
        raise AssertionError("Expected ValueError for negative integer")


def test_static_config_diff_labels_reports_changed_domains() -> None:
    base = AppConfig()
    baseline = _static_config_snapshot(base)
    changed = base.model_copy(deep=True)
    changed.namespace.databases = 3
    changed.load.ramp_up_seconds = 120
    changed.document_schema.fields[0].name = "tenant_code"
    current = _static_config_snapshot(changed)

    labels = _static_config_diff_labels(current, baseline)
    assert "databases" in labels
    assert "ramp-up seconds" in labels
    assert "schema fields" in labels


def test_snapshot_from_persisted_rows_builds_metrics_snapshot() -> None:
    rows = [
        {
            "timestamp_utc": "2026-02-16T10:00:00+00:00",
            "total_ops": 10,
            "error_ops": 1,
            "retried_ops": 2,
            "avg_attempts_per_op": 1.2,
            "avg_latency_ms": 4.0,
            "p95_latency_ms": 8.0,
            "read_ops": 6,
            "insert_ops": 2,
            "update_ops": 1,
            "delete_ops": 1,
            "active_virtual_users": 100,
            "target_virtual_users": 120,
            "paused": False,
        },
        {
            "timestamp_utc": "2026-02-16T10:00:01+00:00",
            "total_ops": 20,
            "error_ops": 2,
            "retried_ops": 3,
            "avg_attempts_per_op": 1.1,
            "avg_latency_ms": 5.0,
            "p95_latency_ms": 9.5,
            "read_ops": 10,
            "insert_ops": 4,
            "update_ops": 4,
            "delete_ops": 2,
            "active_virtual_users": 110,
            "target_virtual_users": 120,
            "paused": False,
        },
    ]

    snapshot = _snapshot_from_persisted_rows(rows)

    assert snapshot.total_ops == 30
    assert snapshot.error_ops == 3
    assert snapshot.success_ops == 27
    assert snapshot.retried_ops == 5
    assert snapshot.active_virtual_users_current == 110
    assert snapshot.p95_latency_ms == 9.5
    assert snapshot.operation_counts["read"] == 16
    assert len(snapshot.timeline) == 2


def test_format_datetime_short_handles_utc_and_missing_values() -> None:
    instant = datetime(2026, 2, 16, 12, 0, 1, tzinfo=UTC)
    assert _format_datetime_short(instant) == "2026-02-16 12:00:01"
    assert _format_datetime_short(None) == "n/a"
