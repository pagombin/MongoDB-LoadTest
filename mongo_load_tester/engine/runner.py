"""Load test lifecycle and worker orchestration."""

from __future__ import annotations

from collections import deque
import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo import MongoClient

from mongo_load_tester.config import AppConfig, OperationMixConfig, RetryConfig
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
    user_offset: int


@dataclass(frozen=True, slots=True)
class RuntimeChangeEvent:
    timestamp: datetime
    message: str


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
        self._max_virtual_users = max(config.load.virtual_users, 1)
        self._target_virtual_users = min(config.load.virtual_users, self._max_virtual_users)
        self._target_duration_seconds = config.load.duration_seconds
        self._operation_mix = config.operation_mix.model_copy(deep=True)
        self._think_time_ms_min = config.load.think_time_ms_min
        self._think_time_ms_max = config.load.think_time_ms_max
        self._target_ops_per_user_per_sec = config.load.target_ops_per_user_per_sec
        self._progress_log_interval_seconds = config.logging.progress_log_interval_seconds
        self._log_operation_failures = config.logging.log_operation_failures
        self._run_started_monotonic: float | None = None
        self._paused_started_monotonic: float | None = None
        self._paused_accumulated_seconds = 0.0
        self._final_active_elapsed_seconds: float | None = None
        self._client: MongoClient | None = None
        self._logger = logger or logging.getLogger("mongo_load_tester.runner")
        self._retry_policy = RetryPolicy.from_config(config.retry)
        self._runtime_change_events: deque[RuntimeChangeEvent] = deque(maxlen=120)

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
    def max_virtual_users(self) -> int:
        with self._state_lock:
            return self._max_virtual_users

    @property
    def target_virtual_users(self) -> int:
        with self._state_lock:
            return self._target_virtual_users

    @property
    def active_virtual_users(self) -> int:
        with self._state_lock:
            active_users, _, _ = self._current_user_levels_locked()
            return active_users

    @property
    def progress_log_interval_seconds(self) -> int:
        with self._state_lock:
            return self._progress_log_interval_seconds

    @property
    def log_operation_failures(self) -> bool:
        with self._state_lock:
            return self._log_operation_failures

    def active_elapsed_seconds(self) -> float:
        with self._state_lock:
            return self._active_elapsed_seconds_locked()

    def operation_mix_normalized(self) -> dict[str, float]:
        with self._state_lock:
            return self._operation_mix.normalized()

    def pacing_snapshot(self) -> tuple[int, int, float | None]:
        with self._state_lock:
            return self._think_time_ms_min, self._think_time_ms_max, self._target_ops_per_user_per_sec

    def retry_policy_snapshot(self) -> RetryPolicy:
        with self._state_lock:
            return self._retry_policy

    def runtime_change_events(self, limit: int = 12) -> list[RuntimeChangeEvent]:
        safe_limit = max(int(limit), 0)
        with self._state_lock:
            if safe_limit == 0:
                return []
            return list(self._runtime_change_events)[-safe_limit:]

    def pause(self) -> bool:
        with self._state_lock:
            if not self._running or self._pause_event.is_set():
                return False
            self._pause_event.set()
            self._paused_started_monotonic = time.monotonic()
            _, target_users, _ = self._current_user_levels_locked()
        self._logger.warning("Load test paused.")
        self._metrics.record_active_users(0, target_users=target_users, paused=True)
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
            resumed_users, target_users, _ = self._current_user_levels_locked()
        self._logger.warning("Load test resumed.")
        self._metrics.record_active_users(resumed_users, target_users=target_users, paused=False)
        return True

    def update_duration_seconds(self, duration_seconds: int) -> int:
        safe_duration = max(int(duration_seconds), 1)
        with self._state_lock:
            self._target_duration_seconds = safe_duration
            self._config.load.duration_seconds = safe_duration
        self._logger.info("Run duration updated to %ds.", safe_duration)
        self._record_runtime_change(f"duration={safe_duration}s")
        return safe_duration

    def update_target_virtual_users(self, virtual_users: int) -> tuple[int, int]:
        requested_users = max(int(virtual_users), 1)
        with self._state_lock:
            max_users = self._max_virtual_users
            applied_users = min(requested_users, max_users)
            self._target_virtual_users = applied_users
        self._logger.info(
            "Live target users updated to %d/%d (requested=%d).",
            applied_users,
            max_users,
            requested_users,
        )
        self._record_runtime_change(f"target_users={applied_users}/{max_users}")
        self._sample_active_users()
        return applied_users, max_users

    def update_operation_mix(
        self,
        read: float,
        insert: float,
        update: float,
        delete: float,
    ) -> OperationMixConfig:
        mix = OperationMixConfig(read=read, insert=insert, update=update, delete=delete)
        with self._state_lock:
            self._operation_mix = mix
            self._config.operation_mix = mix.model_copy(deep=True)
        normalized = mix.normalized()
        self._logger.info(
            "Live operation mix updated read=%.3f insert=%.3f update=%.3f delete=%.3f",
            normalized["read"],
            normalized["insert"],
            normalized["update"],
            normalized["delete"],
        )
        self._record_runtime_change(
            "operation_mix="
            f"{normalized['read'] * 100:.1f}/{normalized['insert'] * 100:.1f}/"
            f"{normalized['update'] * 100:.1f}/{normalized['delete'] * 100:.1f}"
        )
        return mix

    def update_pacing(
        self,
        think_time_ms_min: int,
        think_time_ms_max: int,
        target_ops_per_user_per_sec: float | None,
    ) -> tuple[int, int, float | None]:
        safe_min = max(int(think_time_ms_min), 0)
        safe_max = max(int(think_time_ms_max), 0)
        if safe_min > safe_max:
            raise ValueError("think_time_ms_min must be <= think_time_ms_max")
        safe_target_rate: float | None = None
        if target_ops_per_user_per_sec is not None:
            parsed = float(target_ops_per_user_per_sec)
            if parsed <= 0:
                raise ValueError("target_ops_per_user_per_sec must be > 0 when provided")
            safe_target_rate = parsed
        with self._state_lock:
            self._think_time_ms_min = safe_min
            self._think_time_ms_max = safe_max
            self._target_ops_per_user_per_sec = safe_target_rate
            self._config.load.think_time_ms_min = safe_min
            self._config.load.think_time_ms_max = safe_max
            self._config.load.target_ops_per_user_per_sec = safe_target_rate
        target_rate_text = "unbounded" if safe_target_rate is None else f"{safe_target_rate:.3f}/sec"
        self._logger.info(
            "Live pacing updated think_time_ms=%d-%d target_ops_per_user=%s",
            safe_min,
            safe_max,
            target_rate_text,
        )
        self._record_runtime_change(
            f"pacing={safe_min}-{safe_max}ms, target_ops_per_user={target_rate_text}"
        )
        return safe_min, safe_max, safe_target_rate

    def update_retry_policy(
        self,
        max_attempts: int,
        initial_backoff_ms: int,
        max_backoff_ms: int,
        backoff_multiplier: float,
        jitter_ratio: float,
    ) -> RetryPolicy:
        retry_config = RetryConfig(
            max_attempts=max_attempts,
            initial_backoff_ms=initial_backoff_ms,
            max_backoff_ms=max_backoff_ms,
            backoff_multiplier=backoff_multiplier,
            jitter_ratio=jitter_ratio,
        )
        policy = RetryPolicy.from_config(retry_config)
        with self._state_lock:
            self._retry_policy = policy
            self._config.retry = retry_config
        self._logger.info(
            "Live retry policy updated attempts=%d initial_backoff=%dms max_backoff=%dms multiplier=%.2f jitter=%.2f",
            policy.max_attempts,
            policy.initial_backoff_ms,
            policy.max_backoff_ms,
            policy.backoff_multiplier,
            policy.jitter_ratio,
        )
        self._record_runtime_change(
            f"retry=attempts:{policy.max_attempts},backoff:{policy.initial_backoff_ms}-{policy.max_backoff_ms}ms"
        )
        return policy

    def update_logging_controls(
        self,
        progress_log_interval_seconds: int,
        log_operation_failures: bool,
    ) -> tuple[int, bool]:
        safe_interval = max(int(progress_log_interval_seconds), 1)
        safe_failures = bool(log_operation_failures)
        with self._state_lock:
            self._progress_log_interval_seconds = safe_interval
            self._log_operation_failures = safe_failures
            self._config.logging.progress_log_interval_seconds = safe_interval
            self._config.logging.log_operation_failures = safe_failures
        self._logger.info(
            "Live worker logging updated progress_interval=%ds log_operation_failures=%s",
            safe_interval,
            safe_failures,
        )
        self._record_runtime_change(
            f"worker_logging=interval:{safe_interval}s,operation_failures:{safe_failures}"
        )
        return safe_interval, safe_failures

    def stop(self) -> None:
        self._logger.info("Stop requested by user/UI.")
        self._stop_event.set()

    def run(self) -> None:
        workers: list[threading.Thread] = []
        lifecycle_rng = random.Random(self._config.random_seed + 9_111)
        lifecycle_policy = self.retry_policy_snapshot()
        lifecycle_retry = RetryController(
            policy=lifecycle_policy,
            rng=lifecycle_rng,
            log_warning=self._logger.warning,
        )
        with self._state_lock:
            self._stop_event.clear()
            self._running = True
            self._started_at = datetime.now(UTC)
            self._finished_at = None
            self._last_error = None
            self._started_virtual_users = 0
            self._target_duration_seconds = max(int(self._target_duration_seconds), 1)
            self._target_virtual_users = max(min(self._target_virtual_users, self._max_virtual_users), 1)
            self._run_started_monotonic = time.monotonic()
            self._paused_started_monotonic = None
            self._paused_accumulated_seconds = 0.0
            self._final_active_elapsed_seconds = None
            self._pause_event.clear()
        self._id_registry = DocumentIdRegistry()
        self._metrics.record_active_users(0, target_users=0, paused=False)
        self._logger.info(
            "Load test starting max_users=%d target_users=%d workers=%d duration=%ds ramp_up=%ds",
            self._max_virtual_users,
            self._target_virtual_users,
            self._config.load.worker_threads,
            self._target_duration_seconds,
            self._config.load.ramp_up_seconds,
        )
        try:
            self._client = build_client(self._config.mongo)
            _, ping_attempts = lifecycle_retry.run("admin.ping", lambda: self._client.admin.command("ping"))
            self._logger.info("Connected to MongoDB cluster (ping attempts=%d).", ping_attempts)
            self._seed_initial_documents(self._client)

            assignments = self._build_assignments()
            next_progress_log = time.monotonic() + self.progress_log_interval_seconds
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
                    next_progress_log = time.monotonic() + self.progress_log_interval_seconds
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
        users = self._max_virtual_users
        worker_count = min(self._config.load.worker_threads, users)
        base = users // worker_count
        remainder = users % worker_count
        assignments: list[_WorkerAssignment] = []
        user_offset = 0
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
                    user_offset=user_offset,
                )
            )
            user_offset += simulated_users
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
            operation_selector = OperationSelector(
                self._config.operation_mix,
                rng,
                mix_provider=self._current_operation_mix,
            )
            generator = DocumentGenerator(self._config.document_schema, rng)
            assert self._client is not None
            executor = MongoOperationExecutor(
                client=self._client,
                namespace_selector=namespace_selector,
                operation_selector=operation_selector,
                document_generator=generator,
                id_registry=self._id_registry,
                retry_policy=None,
                logger=worker_logger,
                rng=rng,
                retry_policy_provider=self._current_retry_policy,
            )

            while not self._stop_event.is_set() and not self._duration_reached():
                self._wait_if_paused()
                if self._stop_event.is_set() or self._duration_reached():
                    break
                active_users_for_worker = self._active_users_for_assignment(assignment)
                if active_users_for_worker <= 0:
                    time.sleep(0.05)
                    continue
                think_min, think_max, cycle_target_seconds, log_operation_failures = self._worker_cycle_settings()
                cycle_start = time.perf_counter()
                for _ in range(active_users_for_worker):
                    self._wait_if_paused()
                    if self._stop_event.is_set() or self._duration_reached():
                        break
                    result = executor.execute_once()
                    self._metrics.record(result)
                    if not result.success and log_operation_failures:
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
            policy=self._current_retry_policy(),
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
            active_users, target_users, paused = self._current_user_levels_locked()
        self._metrics.record_active_users(active_users, target_users=target_users, paused=paused)

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
            active_users, target_users, paused = self._current_user_levels_locked()
        self._metrics.record_active_users(active_users, target_users=target_users, paused=paused)

    def _active_users_for_assignment(self, assignment: _WorkerAssignment) -> int:
        with self._state_lock:
            if self._pause_event.is_set():
                return 0
            target_users = min(max(self._target_virtual_users, 0), max(self._started_virtual_users, 0))
            if target_users <= assignment.user_offset:
                return 0
            return min(assignment.simulated_users, target_users - assignment.user_offset)

    def _worker_cycle_settings(self) -> tuple[float, float, float, bool]:
        with self._state_lock:
            think_min = self._think_time_ms_min / 1000.0
            think_max = self._think_time_ms_max / 1000.0
            target_rate = self._target_ops_per_user_per_sec
            cycle_target_seconds = (1.0 / target_rate) if target_rate else 0.0
            log_operation_failures = self._log_operation_failures
        return think_min, think_max, cycle_target_seconds, log_operation_failures

    def _current_retry_policy(self) -> RetryPolicy:
        with self._state_lock:
            return self._retry_policy

    def _current_operation_mix(self) -> OperationMixConfig:
        with self._state_lock:
            return self._operation_mix

    def _current_user_levels_locked(self) -> tuple[int, int, bool]:
        paused = self._pause_event.is_set()
        target_users = min(max(self._target_virtual_users, 0), max(self._started_virtual_users, 0))
        active_users = 0 if paused else target_users
        return active_users, target_users, paused

    def _record_runtime_change(self, message: str) -> None:
        with self._state_lock:
            self._runtime_change_events.append(RuntimeChangeEvent(timestamp=datetime.now(UTC), message=message))
