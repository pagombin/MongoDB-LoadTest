"""Load test lifecycle and worker orchestration."""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo import MongoClient

from mongo_load_tester.config import AppConfig
from mongo_load_tester.engine.client import build_client
from mongo_load_tester.engine.executor import MongoOperationExecutor
from mongo_load_tester.engine.id_registry import DocumentIdRegistry
from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.retry import RetryController, RetryFailure, RetryPolicy
from mongo_load_tester.workload.schema import DocumentGenerator
from mongo_load_tester.workload.selection import NamespaceSelector, OperationSelector


@dataclass(slots=True)
class _WorkerAssignment:
    worker_id: int
    simulated_users: int
    startup_delay_seconds: float


class LoadTestRunner:
    """Coordinates MongoDB load generation and metrics collection."""

    def __init__(self, config: AppConfig, metrics: MetricsStore, logger: logging.Logger | None = None):
        self._config = config
        self._metrics = metrics
        self._id_registry = DocumentIdRegistry()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._state_lock = threading.Lock()
        self._running = False
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._last_error: str | None = None
        self._started_virtual_users = 0
        self._target_duration_seconds = config.load.duration_seconds
        self._run_started_monotonic: float | None = None
        self._paused_started_monotonic: float | None = None
        self._paused_accumulated_seconds = 0.0
        self._final_active_elapsed_seconds: float | None = None
        self._client: MongoClient | None = None
        self._logger = logger or logging.getLogger("mongo_load_tester.runner")
        self._retry_policy = RetryPolicy.from_config(config.retry)

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    @property
    def started_at(self) -> datetime | None:
        with self._state_lock:
            return self._started_at

    @property
    def finished_at(self) -> datetime | None:
        with self._state_lock:
            return self._finished_at

    @property
    def last_error(self) -> str | None:
        with self._state_lock:
            return self._last_error

    def _set_last_error(self, message: str) -> None:
        with self._state_lock:
            if not self._last_error:
                self._last_error = message

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    @property
    def target_duration_seconds(self) -> int:
        with self._state_lock:
            return self._target_duration_seconds

    @property
    def active_virtual_users(self) -> int:
        with self._state_lock:
            if self._pause_event.is_set():
                return 0
            return max(self._started_virtual_users, 0)

    def active_elapsed_seconds(self) -> float:
        with self._state_lock:
            return self._active_elapsed_seconds_locked()

    def pause(self) -> bool:
        with self._state_lock:
            if not self._running or self._pause_event.is_set():
                return False
            self._pause_event.set()
            self._paused_started_monotonic = time.monotonic()
        self._logger.warning("Load test paused.")
        self._metrics.record_active_users(0, paused=True)
        return True

    def resume(self) -> bool:
        with self._state_lock:
            if not self._running or not self._pause_event.is_set():
                return False
            now = time.monotonic()
            if self._paused_started_monotonic is not None:
                self._paused_accumulated_seconds += max(now - self._paused_started_monotonic, 0.0)
            self._paused_started_monotonic = None
            self._pause_event.clear()
            resumed_users = max(self._started_virtual_users, 0)
        self._logger.warning("Load test resumed.")
        self._metrics.record_active_users(resumed_users, paused=False)
        return True

    def update_duration_seconds(self, duration_seconds: int) -> int:
        safe_duration = max(int(duration_seconds), 1)
        with self._state_lock:
            self._target_duration_seconds = safe_duration
        self._logger.info("Run duration updated to %ds.", safe_duration)
        return safe_duration

    def stop(self) -> None:
        self._logger.info("Stop requested by user/UI.")
        self._stop_event.set()

    def run(self) -> None:
        workers: list[threading.Thread] = []
        lifecycle_rng = random.Random(self._config.random_seed + 9_111)
        lifecycle_retry = RetryController(
            policy=self._retry_policy,
            rng=lifecycle_rng,
            log_warning=self._logger.warning,
        )
        with self._state_lock:
            self._running = True
            self._started_at = datetime.now(UTC)
            self._finished_at = None
            self._last_error = None
            self._started_virtual_users = 0
            self._target_duration_seconds = self._config.load.duration_seconds
            self._run_started_monotonic = time.monotonic()
            self._paused_started_monotonic = None
            self._paused_accumulated_seconds = 0.0
            self._final_active_elapsed_seconds = None
            self._pause_event.clear()
        self._metrics.record_active_users(0, paused=False)
        self._logger.info(
            "Load test starting users=%d workers=%d duration=%ds ramp_up=%ds",
            self._config.load.virtual_users,
            self._config.load.worker_threads,
            self._config.load.duration_seconds,
            self._config.load.ramp_up_seconds,
        )
        try:
            self._client = build_client(self._config.mongo)
            _, ping_attempts = lifecycle_retry.run("admin.ping", lambda: self._client.admin.command("ping"))
            self._logger.info("Connected to MongoDB cluster (ping attempts=%d).", ping_attempts)
            self._seed_initial_documents(self._client)

            assignments = self._build_assignments()
            next_progress_log = time.monotonic() + self._config.logging.progress_log_interval_seconds
            for assignment in assignments:
                thread = threading.Thread(
                    target=self._worker_loop,
                    args=(assignment,),
                    daemon=True,
                    name=f"load-worker-{assignment.worker_id}",
                )
                thread.start()
                workers.append(thread)
            self._logger.info("Started %d worker thread(s).", len(workers))

            while not self._stop_event.is_set() and any(thread.is_alive() for thread in workers):
                self._sample_active_users()
                if self._duration_reached():
                    self._stop_event.set()
                    break
                if time.monotonic() >= next_progress_log:
                    snapshot = self._metrics.snapshot()
                    self._logger.info(
                        "Progress total_ops=%d success=%d errors=%d ops_per_sec=%.2f p95_ms=%.2f active_users=%d paused=%s",
                        snapshot.total_ops,
                        snapshot.success_ops,
                        snapshot.error_ops,
                        snapshot.ops_per_second,
                        snapshot.p95_latency_ms,
                        snapshot.active_virtual_users_current,
                        self.is_paused,
                    )
                    next_progress_log = time.monotonic() + self._config.logging.progress_log_interval_seconds
                time.sleep(0.2)
        except BaseException as exc:  # pragma: no cover - runtime safety
            self._set_last_error(str(exc))
            self._logger.exception("Load test run failed.")
        finally:
            self._stop_event.set()
            for thread in workers:
                thread.join(timeout=5)
            if self._client is not None:
                self._client.close()
            self._sample_active_users()
            with self._state_lock:
                if self._paused_started_monotonic is not None:
                    self._paused_accumulated_seconds += max(time.monotonic() - self._paused_started_monotonic, 0.0)
                    self._paused_started_monotonic = None
                self._final_active_elapsed_seconds = self._active_elapsed_seconds_locked()
                self._running = False
                self._finished_at = datetime.now(UTC)
                self._started_virtual_users = 0
                self._pause_event.clear()
            snapshot = self._metrics.snapshot()
            self._logger.info(
                "Load test finished total_ops=%d success=%d errors=%d avg_latency_ms=%.2f p95_ms=%.2f active_elapsed=%.2fs",
                snapshot.total_ops,
                snapshot.success_ops,
                snapshot.error_ops,
                snapshot.avg_latency_ms,
                snapshot.p95_latency_ms,
                self.active_elapsed_seconds(),
            )
            if self.last_error:
                self._logger.error("Run completed with captured failure: %s", self.last_error)

    def _build_assignments(self) -> list[_WorkerAssignment]:
        users = self._config.load.virtual_users
        worker_count = min(self._config.load.worker_threads, users)
        base = users // worker_count
        remainder = users % worker_count
        assignments: list[_WorkerAssignment] = []
        for index in range(worker_count):
            simulated_users = base + (1 if index < remainder else 0)
            startup_delay_seconds = 0.0
            if self._config.load.ramp_up_seconds > 0:
                startup_delay_seconds = (self._config.load.ramp_up_seconds * index) / max(worker_count, 1)
            assignments.append(
                _WorkerAssignment(
                    worker_id=index,
                    simulated_users=simulated_users,
                    startup_delay_seconds=startup_delay_seconds,
                )
            )
        return assignments

    def _worker_loop(self, assignment: _WorkerAssignment) -> None:
        worker_logger = self._logger.getChild(f"worker.{assignment.worker_id}")
        worker_users_registered = False
        try:
            if assignment.startup_delay_seconds > 0:
                startup_remaining = assignment.startup_delay_seconds
                while startup_remaining > 0 and not self._stop_event.is_set():
                    sleep_chunk = min(0.2, startup_remaining)
                    time.sleep(sleep_chunk)
                    startup_remaining -= sleep_chunk
            if self._stop_event.is_set():
                return

            seed = self._config.random_seed + assignment.worker_id * 10_007
            rng = random.Random(seed)
            worker_logger.info(
                "Worker started simulated_users=%d startup_delay=%.2fs",
                assignment.simulated_users,
                assignment.startup_delay_seconds,
            )
            self._adjust_started_users(assignment.simulated_users)
            worker_users_registered = True
            namespace_selector = NamespaceSelector(self._config.namespace, rng)
            operation_selector = OperationSelector(self._config.operation_mix, rng)
            generator = DocumentGenerator(self._config.document_schema, rng)
            assert self._client is not None
            executor = MongoOperationExecutor(
                client=self._client,
                namespace_selector=namespace_selector,
                operation_selector=operation_selector,
                document_generator=generator,
                id_registry=self._id_registry,
                retry_policy=self._retry_policy,
                logger=worker_logger,
                rng=rng,
            )

            think_min = self._config.load.think_time_ms_min / 1000.0
            think_max = self._config.load.think_time_ms_max / 1000.0
            target_rate = self._config.load.target_ops_per_user_per_sec
            cycle_target_seconds = (1.0 / target_rate) if target_rate else 0.0

            while not self._stop_event.is_set() and not self._duration_reached():
                self._wait_if_paused()
                if self._stop_event.is_set() or self._duration_reached():
                    break
                cycle_start = time.perf_counter()
                for _ in range(assignment.simulated_users):
                    self._wait_if_paused()
                    if self._stop_event.is_set() or self._duration_reached():
                        break
                    result = executor.execute_once()
                    self._metrics.record(result)
                    if not result.success and self._config.logging.log_operation_failures:
                        worker_logger.error(
                            "Operation failed op=%s ns=%s.%s attempts=%d latency_ms=%.2f error=%s",
                            result.operation.value,
                            result.database,
                            result.collection,
                            result.attempts,
                            result.latency_ms,
                            result.error,
                        )
                    if think_max > 0:
                        time.sleep(rng.uniform(think_min, think_max))

                if cycle_target_seconds > 0:
                    cycle_elapsed = time.perf_counter() - cycle_start
                    if cycle_elapsed < cycle_target_seconds:
                        time.sleep(cycle_target_seconds - cycle_elapsed)
        except BaseException as exc:  # pragma: no cover - runtime safety
            self._set_last_error(f"Worker {assignment.worker_id} crashed: {exc}")
            worker_logger.exception("Worker crashed unexpectedly.")
            self._stop_event.set()
        finally:
            if worker_users_registered:
                self._adjust_started_users(-assignment.simulated_users)
            worker_logger.info("Worker exiting.")

    def _seed_initial_documents(self, client: MongoClient) -> None:
        seed_count = self._config.load.initial_seed_per_collection
        if seed_count <= 0:
            self._logger.info("Initial seeding skipped (initial_seed_per_collection=%d).", seed_count)
            return

        rng = random.Random(self._config.random_seed)
        retry_rng = random.Random(self._config.random_seed + 123_456)
        retry_controller = RetryController(
            policy=self._retry_policy,
            rng=retry_rng,
            log_warning=self._logger.warning,
        )
        namespace_selector = NamespaceSelector(self._config.namespace, rng)
        generator = DocumentGenerator(self._config.document_schema, rng)
        batch_size = 500
        total_targets = len(namespace_selector.targets)
        self._logger.info(
            "Starting initial seed per_collection=%d targets=%d batch_size=%d",
            seed_count,
            total_targets,
            batch_size,
        )

        for target in namespace_selector.targets:
            remaining = seed_count
            collection = client[target.database][target.collection]
            inserted_for_target = 0
            while remaining > 0 and not self._stop_event.is_set():
                current_batch_size = min(batch_size, remaining)
                docs = [generator.generate_document() for _ in range(current_batch_size)]
                operation_name = (
                    f"seed insert_many {target.database}.{target.collection} "
                    f"batch_size={current_batch_size}"
                )
                try:
                    insert_result, _ = retry_controller.run(
                        operation_name=operation_name,
                        func=lambda docs=docs, collection=collection: collection.insert_many(docs, ordered=False),
                    )
                except RetryFailure as exc:
                    self._logger.error(
                        "Seeding failed namespace=%s.%s attempts=%d error=%s",
                        target.database,
                        target.collection,
                        exc.attempts,
                        exc.last_exception,
                    )
                    raise
                for inserted_id in insert_result.inserted_ids:
                    self._id_registry.add_id(target.database, target.collection, inserted_id)
                remaining -= current_batch_size
                inserted_for_target += current_batch_size
            self._logger.info(
                "Seeded namespace=%s.%s inserted=%d",
                target.database,
                target.collection,
                inserted_for_target,
            )

    def _adjust_started_users(self, delta: int) -> None:
        with self._state_lock:
            self._started_virtual_users = max(self._started_virtual_users + delta, 0)
            paused = self._pause_event.is_set()
            active_users = 0 if paused else self._started_virtual_users
        self._metrics.record_active_users(active_users, paused=paused)

    def _wait_if_paused(self) -> None:
        while self._pause_event.is_set() and not self._stop_event.is_set():
            self._sample_active_users()
            time.sleep(0.1)

    def _duration_reached(self) -> bool:
        with self._state_lock:
            elapsed = self._active_elapsed_seconds_locked()
            return elapsed >= self._target_duration_seconds

    def _active_elapsed_seconds_locked(self) -> float:
        if self._final_active_elapsed_seconds is not None:
            return self._final_active_elapsed_seconds
        if self._run_started_monotonic is None:
            return 0.0
        now = time.monotonic()
        paused_total = self._paused_accumulated_seconds
        if self._pause_event.is_set() and self._paused_started_monotonic is not None:
            paused_total += max(now - self._paused_started_monotonic, 0.0)
        return max(now - self._run_started_monotonic - paused_total, 0.0)

    def _sample_active_users(self) -> None:
        with self._state_lock:
            paused = self._pause_event.is_set()
            active_users = 0 if paused else self._started_virtual_users
        self._metrics.record_active_users(active_users, paused=paused)
