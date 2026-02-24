"""Load test lifecycle and worker orchestration."""

from __future__ import annotations

from collections import deque
import logging
import random
import threading
import time
from dataclasses import dataclass, field as dataclass_field
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
    dismissed: threading.Event = dataclass_field(default_factory=threading.Event)

_SCALE_UP_STAGGER_SECONDS = 0.5
_ERROR_RATE_WARNING_THRESHOLD = 0.10


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
        self._target_worker_threads = max(config.load.worker_threads, 1)
        self._max_worker_threads = max(config.load.max_worker_threads, 1)
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
        self._scaling_alerts: deque[str] = deque(maxlen=20)
        self._worker_assignments: list[_WorkerAssignment] = []
        self._running_worker_ids: set[int] = set()

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
    def target_worker_threads(self) -> int:
        with self._state_lock:
            return self._target_worker_threads

    @property
    def worker_thread_pool_size(self) -> int:
        with self._state_lock:
            return len(self._worker_assignments)

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

    def drain_scaling_alerts(self) -> list[str]:
        """Return and clear any pending scaling alert messages."""
        with self._state_lock:
            alerts = list(self._scaling_alerts)
            self._scaling_alerts.clear()
            return alerts

    def snapshot_config(self) -> AppConfig:
        with self._state_lock:
            config = self._config.model_copy(deep=True)
            config.load.virtual_users = self._max_virtual_users
            config.load.worker_threads = self._target_worker_threads
            config.load.max_worker_threads = self._max_worker_threads
            config.load.duration_seconds = self._target_duration_seconds
            config.operation_mix = self._operation_mix.model_copy(deep=True)
            config.load.think_time_ms_min = self._think_time_ms_min
            config.load.think_time_ms_max = self._think_time_ms_max
            config.load.target_ops_per_user_per_sec = self._target_ops_per_user_per_sec
            config.retry = RetryConfig(
                max_attempts=self._retry_policy.max_attempts,
                initial_backoff_ms=self._retry_policy.initial_backoff_ms,
                max_backoff_ms=self._retry_policy.max_backoff_ms,
                backoff_multiplier=self._retry_policy.backoff_multiplier,
                jitter_ratio=self._retry_policy.jitter_ratio,
            )
            config.logging.progress_log_interval_seconds = self._progress_log_interval_seconds
            config.logging.log_operation_failures = self._log_operation_failures
            return config

    def apply_config(self, config: AppConfig, record_change: bool = False) -> None:
        with self._state_lock:
            self._config = config.model_copy(deep=True)
            self._max_virtual_users = max(config.load.virtual_users, 1)
            self._max_worker_threads = max(config.load.max_worker_threads, 1)
            self._target_virtual_users = max(min(config.load.virtual_users, self._max_virtual_users), 1)
            self._target_worker_threads = max(min(config.load.worker_threads, self._max_virtual_users, self._max_worker_threads), 1)
            self._target_duration_seconds = max(config.load.duration_seconds, 1)
            self._operation_mix = config.operation_mix.model_copy(deep=True)
            self._think_time_ms_min = config.load.think_time_ms_min
            self._think_time_ms_max = config.load.think_time_ms_max
            self._target_ops_per_user_per_sec = config.load.target_ops_per_user_per_sec
            self._retry_policy = RetryPolicy.from_config(config.retry)
            self._progress_log_interval_seconds = config.logging.progress_log_interval_seconds
            self._log_operation_failures = config.logging.log_operation_failures
            if self._running:
                self._ensure_worker_assignments_locked(self._target_worker_threads)
            self._rebalance_assignments_locked()
        if record_change:
            self._record_runtime_change("config_applied_from_metadata")
        self._sample_active_users()

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
            self._max_virtual_users = requested_users
            self._target_virtual_users = requested_users
            self._target_worker_threads = min(max(self._target_worker_threads, 1), self._max_virtual_users)
            self._config.load.virtual_users = requested_users
            if self._running:
                self._ensure_worker_assignments_locked(self._target_worker_threads)
            self._rebalance_assignments_locked()
            max_users = self._max_virtual_users
            applied_users = self._target_virtual_users
        self._logger.info(
            "Live target users updated to %d/%d.",
            applied_users,
            max_users,
        )
        self._record_runtime_change(f"target_users={applied_users}/{max_users}")
        self._sample_active_users()
        return applied_users, max_users

    def update_worker_threads(self, worker_threads: int) -> tuple[int, int]:
        requested_threads = max(int(worker_threads), 1)
        with self._state_lock:
            capped = min(requested_threads, self._max_worker_threads, self._max_virtual_users)
            was_capped = capped < requested_threads
            capped_by_users = requested_threads > self._max_virtual_users
            capped_by_limit = requested_threads > self._max_worker_threads
            max_users = self._max_virtual_users
            max_threads = self._max_worker_threads
            old_target = self._target_worker_threads
            self._target_worker_threads = capped
            self._config.load.worker_threads = capped
            if self._running:
                if capped < old_target:
                    self._dismiss_excess_workers_locked(capped)
                self._ensure_worker_assignments_locked(capped)
            self._rebalance_assignments_locked()
            pool_size = len(self._worker_assignments)
        if was_capped:
            reasons: list[str] = []
            if capped_by_users:
                reasons.append(f"virtual users is {max_users}")
            if capped_by_limit:
                reasons.append(f"max_worker_threads is {max_threads}")
            reason_text = " and ".join(reasons)
            cap_msg = (
                f"Worker threads capped from {requested_threads} to {capped} "
                f"because {reason_text}. "
            )
            if capped_by_users and not capped_by_limit:
                cap_msg += "Increase target users first to allow more workers."
            elif capped_by_limit and not capped_by_users:
                cap_msg += "Adjust max_worker_threads in your config to raise the hard limit."
            else:
                cap_msg += "Increase target users and/or max_worker_threads to allow more workers."
            self._logger.warning(cap_msg)
            self._record_runtime_change(
                f"worker_threads={capped} (capped from {requested_threads})"
            )
            self._push_scaling_alert(cap_msg)
        else:
            self._logger.info(
                "Live worker thread target updated to %d (requested=%d, pool=%d).",
                capped,
                requested_threads,
                pool_size,
            )
            self._record_runtime_change(f"worker_threads={capped}")
        self._sample_active_users()
        return capped, max(pool_size, capped)

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
        workers: dict[int, threading.Thread] = {}
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
            self._target_worker_threads = max(self._target_worker_threads, 1)
            self._run_started_monotonic = time.monotonic()
            self._paused_started_monotonic = None
            self._paused_accumulated_seconds = 0.0
            self._final_active_elapsed_seconds = None
            self._pause_event.clear()
            self._running_worker_ids.clear()
            self._worker_assignments = []
            self._id_registry = DocumentIdRegistry()
            log_max_users = self._max_virtual_users
            log_target_users = self._target_virtual_users
            log_workers = self._target_worker_threads
            log_duration = self._target_duration_seconds
        self._metrics.record_active_users(0, target_users=0, paused=False)
        self._logger.info(
            "Load test starting max_users=%d target_users=%d workers=%d duration=%ds ramp_up=%ds",
            log_max_users,
            log_target_users,
            log_workers,
            log_duration,
            self._config.load.ramp_up_seconds,
        )
        try:
            self._client = build_client(self._config.mongo)
            _, ping_attempts = lifecycle_retry.run("admin.ping", lambda: self._client.admin.command("ping"))
            self._logger.info("Connected to MongoDB cluster (ping attempts=%d).", ping_attempts)
            self._seed_initial_documents(self._client)

            assignments = self._build_assignments()
            with self._state_lock:
                self._worker_assignments = assignments
                self._ensure_worker_assignments_locked(self._target_worker_threads)
                self._rebalance_assignments_locked()
            next_progress_log = time.monotonic() + self.progress_log_interval_seconds
            self._reconcile_worker_pool(workers)
            self._logger.info("Started %d worker thread(s).", len(workers))

            while not self._stop_event.is_set() and any(thread.is_alive() for thread in workers.values()):
                self._reconcile_worker_pool(workers)
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
                self._stop_event.wait(timeout=0.2)
        except BaseException as exc:  # pragma: no cover - runtime safety
            self._set_last_error(str(exc))
            self._logger.exception("Load test run failed.")
        finally:
            self._stop_event.set()
            for thread in workers.values():
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
                self._running_worker_ids.clear()
                self._worker_assignments = []
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
        worker_count = min(max(self._target_worker_threads, 1), users)
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
            self._register_running_worker(assignment.worker_id)
            worker_users_registered = True
            namespace_selector = NamespaceSelector(self._config.namespace, rng)
            operation_selector = OperationSelector(
                self._config.operation_mix,
                rng,
                mix_provider=self._current_operation_mix,
            )
            generator = DocumentGenerator(self._config.document_schema, rng)
            if self._client is None:
                raise RuntimeError("MongoClient unavailable; cannot start worker.")
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
                query_targets=self._config.document_schema.query_targets,
            )

            while not self._stop_event.is_set() and not self._duration_reached() and not assignment.dismissed.is_set():
                self._wait_if_paused()
                if self._stop_event.is_set() or self._duration_reached() or assignment.dismissed.is_set():
                    break
                active_users_for_worker = self._active_users_for_assignment(assignment)
                if active_users_for_worker <= 0:
                    time.sleep(0.05)
                    continue
                think_min, think_max, cycle_target_seconds, log_operation_failures = self._worker_cycle_settings()
                cycle_start = time.perf_counter()
                for _ in range(active_users_for_worker):
                    self._wait_if_paused()
                    if self._stop_event.is_set() or self._duration_reached() or assignment.dismissed.is_set():
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
                self._unregister_running_worker(assignment.worker_id)
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
            self._create_query_target_indexes(collection, target.database, target.collection)

    def _create_query_target_indexes(self, collection: Any, database: str, coll: str) -> None:
        qt = self._config.document_schema.query_targets
        if not qt.enabled or not qt.create_indexes:
            return
        all_fields: set[str] = set()
        all_fields.update(qt.read_filter_fields)
        all_fields.update(qt.update_filter_fields)
        all_fields.update(qt.delete_filter_fields)
        for field_name in sorted(all_fields):
            try:
                collection.create_index(field_name)
                self._logger.info(
                    "Created query target index on %s.%s field=%s",
                    database, coll, field_name,
                )
            except Exception:
                self._logger.warning(
                    "Failed to create index on %s.%s field=%s (may already exist or insufficient privileges).",
                    database, coll, field_name,
                    exc_info=True,
                )

    def _register_running_worker(self, worker_id: int) -> None:
        with self._state_lock:
            self._running_worker_ids.add(worker_id)
            active_users, target_users, paused = self._current_user_levels_locked()
        self._metrics.record_active_users(active_users, target_users=target_users, paused=paused)

    def _unregister_running_worker(self, worker_id: int) -> None:
        with self._state_lock:
            self._running_worker_ids.discard(worker_id)
            active_users, target_users, paused = self._current_user_levels_locked()
        self._metrics.record_active_users(active_users, target_users=target_users, paused=paused)

    def _wait_if_paused(self) -> None:
        while self._pause_event.is_set() and not self._stop_event.is_set():
            self._stop_event.wait(timeout=0.5)

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
            if not self._running:
                return
            active_users, target_users, paused = self._current_user_levels_locked()
        self._metrics.record_active_users(active_users, target_users=target_users, paused=paused)

    def _active_users_for_assignment(self, assignment: _WorkerAssignment) -> int:
        with self._state_lock:
            if self._pause_event.is_set():
                return 0
            target_users = min(max(self._target_virtual_users, 0), self._available_virtual_capacity_locked())
            if target_users <= assignment.user_offset:
                return 0
            return min(assignment.simulated_users, target_users - assignment.user_offset)

    def _worker_cycle_settings(self) -> tuple[float, float, float, bool]:
        with self._state_lock:
            think_min = self._think_time_ms_min / 1000.0
            think_max = self._think_time_ms_max / 1000.0
            target_rate = self._target_ops_per_user_per_sec
            cycle_target_seconds = (1.0 / target_rate) if target_rate is not None else 0.0
            log_operation_failures = self._log_operation_failures
        return think_min, think_max, cycle_target_seconds, log_operation_failures

    def _current_retry_policy(self) -> RetryPolicy:
        with self._state_lock:
            return self._retry_policy

    def _current_operation_mix(self) -> OperationMixConfig:
        with self._state_lock:
            return self._operation_mix.model_copy()

    def _current_user_levels_locked(self) -> tuple[int, int, bool]:
        paused = self._pause_event.is_set()
        target_users = min(max(self._target_virtual_users, 0), self._available_virtual_capacity_locked())
        active_users = 0 if paused else target_users
        return active_users, target_users, paused

    def _available_virtual_capacity_locked(self) -> int:
        if not self._worker_assignments or not self._running_worker_ids:
            return 0
        return sum(
            assignment.simulated_users
            for assignment in self._worker_assignments
            if assignment.worker_id in self._running_worker_ids
        )

    def _dismiss_excess_workers_locked(self, new_target: int) -> None:
        """Signal workers beyond new_target to exit gracefully."""
        for assignment in self._worker_assignments:
            if assignment.worker_id >= new_target:
                if not assignment.dismissed.is_set():
                    assignment.dismissed.set()
                    self._logger.info("Dismissing worker %d (scale-down to %d).", assignment.worker_id, new_target)

    def _rebalance_assignments_locked(self) -> None:
        if not self._worker_assignments:
            return
        active_threads = min(
            max(self._target_worker_threads, 1),
            len(self._worker_assignments),
            max(self._max_virtual_users, 1),
        )
        total_users = max(self._max_virtual_users, 1)
        base = total_users // active_threads
        remainder = total_users % active_threads
        user_offset = 0
        for index, assignment in enumerate(self._worker_assignments):
            if index < active_threads:
                assigned_users = base + (1 if index < remainder else 0)
                assignment.simulated_users = assigned_users
                assignment.user_offset = user_offset
                user_offset += assigned_users
            else:
                assignment.simulated_users = 0
                assignment.user_offset = user_offset

    def _ensure_worker_assignments_locked(self, desired_threads: int) -> None:
        desired = max(int(desired_threads), 1)
        while len(self._worker_assignments) < desired:
            worker_id = len(self._worker_assignments)
            self._worker_assignments.append(
                _WorkerAssignment(
                    worker_id=worker_id,
                    simulated_users=0,
                    startup_delay_seconds=0.0,
                    user_offset=0,
                )
            )

    def _reconcile_worker_pool(self, workers: dict[int, threading.Thread]) -> None:
        if self._stop_event.is_set() or self._duration_reached():
            return
        assignments_to_start: list[_WorkerAssignment] = []
        dismissed_ids: list[int] = []
        with self._state_lock:
            desired_threads = min(
                max(self._target_worker_threads, 1),
                max(self._max_virtual_users, 1),
                max(self._max_worker_threads, 1),
            )
            self._ensure_worker_assignments_locked(desired_threads)
            self._rebalance_assignments_locked()
            for assignment in self._worker_assignments:
                if assignment.dismissed.is_set():
                    thread = workers.get(assignment.worker_id)
                    if thread is not None and not thread.is_alive():
                        dismissed_ids.append(assignment.worker_id)
                    continue
                if assignment.worker_id >= desired_threads:
                    continue
                thread = workers.get(assignment.worker_id)
                if thread is None or not thread.is_alive():
                    assignments_to_start.append(assignment)
        for wid in dismissed_ids:
            workers.pop(wid, None)
        pre_scale_error_ops = self._metrics.snapshot().error_ops if assignments_to_start else 0
        pre_scale_total_ops = self._metrics.snapshot().total_ops if assignments_to_start else 0
        for idx, assignment in enumerate(assignments_to_start):
            if self._stop_event.is_set():
                break
            if idx > 0:
                time.sleep(_SCALE_UP_STAGGER_SECONDS)
            thread = threading.Thread(
                target=self._worker_loop,
                args=(assignment,),
                daemon=True,
                name=f"load-worker-{assignment.worker_id}",
            )
            thread.start()
            workers[assignment.worker_id] = thread
        if len(assignments_to_start) > 1:
            self._schedule_error_rate_check(pre_scale_error_ops, pre_scale_total_ops, len(assignments_to_start))

    def _push_scaling_alert(self, message: str) -> None:
        with self._state_lock:
            self._scaling_alerts.append(message)

    def _schedule_error_rate_check(self, pre_error_ops: int, pre_total_ops: int, added_workers: int) -> None:
        """After a scale-up, check error rate a few seconds later and warn if it spiked."""
        def _check():
            time.sleep(5.0)
            if not self._running:
                return
            snapshot = self._metrics.snapshot()
            new_errors = snapshot.error_ops - pre_error_ops
            new_ops = snapshot.total_ops - pre_total_ops
            if new_ops <= 0:
                return
            new_error_rate = new_errors / new_ops
            if new_error_rate >= _ERROR_RATE_WARNING_THRESHOLD:
                pct = new_error_rate * 100
                msg = (
                    f"Error rate spiked to {pct:.1f}% after scaling to "
                    f"{self._target_worker_threads} workers. Consider reducing "
                    f"worker count or checking MongoDB cluster capacity."
                )
                self._logger.warning(msg)
                self._record_runtime_change(f"WARNING: error rate {pct:.1f}% after scaling workers")
                self._push_scaling_alert(msg)
        threading.Thread(target=_check, daemon=True, name="error-rate-check").start()

    def _record_runtime_change(self, message: str) -> None:
        with self._state_lock:
            self._runtime_change_events.append(RuntimeChangeEvent(timestamp=datetime.now(UTC), message=message))
