"""Distributed metrics: workers write per-second summaries to MongoDB, dashboard aggregates."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from pymongo.collection import Collection

from mongo_load_tester.engine.metrics import MetricsStore

DISTRIBUTED_METRICS_COLLECTION = "load_test_live_metrics"

_PROJECTION = {
    "_id": 0,
    "worker_id": 1,
    "timestamp": 1,
    "total_ops": 1,
    "success_ops": 1,
    "error_ops": 1,
    "retried_ops": 1,
    "ops_per_second": 1,
    "avg_latency_ms": 1,
    "p50_latency_ms": 1,
    "p95_latency_ms": 1,
    "p99_latency_ms": 1,
    "active_virtual_users_current": 1,
    "elapsed_seconds": 1,
    "operation_counts": 1,
    "error_categories": 1,
}


def ensure_distributed_indexes(collection: Collection, logger: logging.Logger | None = None) -> None:
    """Create indexes for distributed metrics queries. Safe to call multiple times."""
    _logger = logger or logging.getLogger("mongo_load_tester.distributed")
    try:
        collection.create_index(
            [("load_test_id", 1), ("worker_id", 1)],
            unique=True,
            name="idx_load_test_worker",
        )
        collection.create_index(
            "load_test_id",
            name="idx_load_test_id",
        )
        _logger.info("Distributed metrics indexes verified.")
    except Exception:
        _logger.warning("Failed to create distributed metrics indexes.", exc_info=True)


class MetricsPublisher:
    """Periodically publishes local MetricsStore snapshots to a shared MongoDB collection.

    Run this in each headless worker process. The dashboard's MetricsAggregator
    reads from the same collection to build a unified view.
    """

    def __init__(
        self,
        metrics_store: MetricsStore,
        collection: Collection,
        load_test_id: str,
        worker_id: str,
        interval_seconds: float = 1.0,
        logger: logging.Logger | None = None,
    ):
        self._metrics = metrics_store
        self._collection = collection
        self._load_test_id = load_test_id
        self._worker_id = worker_id
        self._interval = interval_seconds
        self._logger = logger or logging.getLogger("mongo_load_tester.distributed")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_total_ops = 0
        ensure_distributed_indexes(collection, self._logger)

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="metrics-publisher")
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._publish_once()

    def _loop(self) -> None:
        _consecutive_failures = 0
        while not self._stop_event.is_set():
            try:
                self._publish_once()
                _consecutive_failures = 0
            except Exception as exc:
                _consecutive_failures += 1
                msg = str(exc).split("\n")[0][:200]
                if _consecutive_failures <= 3:
                    self._logger.warning("Metrics publish failed: %s", msg)
                elif _consecutive_failures == 4:
                    self._logger.warning("Metrics publish failing repeatedly (%s). Suppressing further warnings until recovery.", msg)
                backoff = min(self._interval * _consecutive_failures, 30.0)
                self._stop_event.wait(backoff)
                continue
            self._stop_event.wait(self._interval)

    def _publish_once(self) -> None:
        snapshot = self._metrics.snapshot()
        if snapshot.total_ops == self._last_total_ops and snapshot.total_ops > 0:
            return
        self._last_total_ops = snapshot.total_ops
        now = datetime.now(UTC)
        doc = {
            "load_test_id": self._load_test_id,
            "worker_id": self._worker_id,
            "timestamp": now,
            "total_ops": snapshot.total_ops,
            "success_ops": snapshot.success_ops,
            "error_ops": snapshot.error_ops,
            "retried_ops": snapshot.retried_ops,
            "ops_per_second": snapshot.ops_per_second,
            "avg_latency_ms": snapshot.avg_latency_ms,
            "p50_latency_ms": snapshot.p50_latency_ms,
            "p95_latency_ms": snapshot.p95_latency_ms,
            "p99_latency_ms": snapshot.p99_latency_ms,
            "active_virtual_users_current": snapshot.active_virtual_users_current,
            "elapsed_seconds": snapshot.elapsed_seconds,
            "operation_counts": snapshot.operation_counts,
            "error_categories": snapshot.error_categories,
        }
        self._collection.update_one(
            {"load_test_id": self._load_test_id, "worker_id": self._worker_id},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )


class MetricsAggregator:
    """Reads distributed metrics from MongoDB and aggregates into a unified view.

    Run this in the dashboard process. It reads the latest snapshot from each
    worker and merges them into aggregate KPIs.
    """

    def __init__(
        self,
        collection: Collection,
        load_test_id: str,
        logger: logging.Logger | None = None,
    ):
        self._collection = collection
        self._load_test_id = load_test_id
        self._logger = logger or logging.getLogger("mongo_load_tester.distributed")
        ensure_distributed_indexes(collection, self._logger)

    def aggregate(self) -> dict[str, Any] | None:
        """Read all worker snapshots and merge into aggregate metrics."""
        cursor = self._collection.find(
            {"load_test_id": self._load_test_id},
            projection=_PROJECTION,
            hint="idx_load_test_id",
        )
        workers = list(cursor)
        if not workers:
            return None

        total_ops = sum(w.get("total_ops", 0) for w in workers)
        success_ops = sum(w.get("success_ops", 0) for w in workers)
        error_ops = sum(w.get("error_ops", 0) for w in workers)
        retried_ops = sum(w.get("retried_ops", 0) for w in workers)
        total_ops_per_sec = sum(w.get("ops_per_second", 0) for w in workers)
        active_users = sum(w.get("active_virtual_users_current", 0) for w in workers)

        weighted_latency = sum(
            w.get("avg_latency_ms", 0) * w.get("total_ops", 0)
            for w in workers
        )
        avg_latency = weighted_latency / total_ops if total_ops else 0.0

        p50s = [w.get("p50_latency_ms", 0) for w in workers if w.get("total_ops", 0) > 0]
        p95s = [w.get("p95_latency_ms", 0) for w in workers if w.get("total_ops", 0) > 0]
        p99s = [w.get("p99_latency_ms", 0) for w in workers if w.get("total_ops", 0) > 0]

        merged_op_counts: dict[str, int] = defaultdict(int)
        merged_errors: dict[str, int] = defaultdict(int)
        for w in workers:
            for op, count in w.get("operation_counts", {}).items():
                merged_op_counts[op] += count
            for cat, count in w.get("error_categories", {}).items():
                merged_errors[cat] += count

        return {
            "worker_count": len(workers),
            "total_ops": total_ops,
            "success_ops": success_ops,
            "error_ops": error_ops,
            "retried_ops": retried_ops,
            "ops_per_second": total_ops_per_sec,
            "avg_latency_ms": avg_latency,
            "p50_latency_ms": max(p50s) if p50s else 0.0,
            "p95_latency_ms": max(p95s) if p95s else 0.0,
            "p99_latency_ms": max(p99s) if p99s else 0.0,
            "active_virtual_users": active_users,
            "operation_counts": dict(merged_op_counts),
            "error_categories": dict(merged_errors),
            "workers": [
                {
                    "worker_id": w.get("worker_id", "?"),
                    "ops_per_second": w.get("ops_per_second", 0),
                    "total_ops": w.get("total_ops", 0),
                    "p95_latency_ms": w.get("p95_latency_ms", 0),
                    "timestamp": w.get("timestamp"),
                }
                for w in workers
            ],
        }

    def clear_worker_metrics(self) -> int:
        """Remove all worker documents for this load test. Uses the load_test_id index."""
        result = self._collection.delete_many(
            {"load_test_id": self._load_test_id},
        )
        return result.deleted_count
