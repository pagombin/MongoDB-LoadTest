"""Load test lifecycle and worker orchestration."""

from __future__ import annotations

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
from mongo_load_tester.workload.schema import DocumentGenerator
from mongo_load_tester.workload.selection import NamespaceSelector, OperationSelector


@dataclass(slots=True)
class _WorkerAssignment:
    worker_id: int
    simulated_users: int
    startup_delay_seconds: float


class LoadTestRunner:
    """Coordinates MongoDB load generation and metrics collection."""

    def __init__(self, config: AppConfig, metrics: MetricsStore):
        self._config = config
        self._metrics = metrics
        self._id_registry = DocumentIdRegistry()
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._running = False
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._last_error: str | None = None
        self._client: MongoClient | None = None

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

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        workers: list[threading.Thread] = []
        with self._state_lock:
            self._running = True
            self._started_at = datetime.now(UTC)
            self._finished_at = None
            self._last_error = None
        try:
            self._client = build_client(self._config.mongo)
            # Fail fast if cluster is unreachable.
            self._client.admin.command("ping")
            self._seed_initial_documents(self._client)

            assignments = self._build_assignments()
            run_deadline = time.monotonic() + self._config.load.duration_seconds
            for assignment in assignments:
                thread = threading.Thread(
                    target=self._worker_loop,
                    args=(assignment, run_deadline),
                    daemon=True,
                    name=f"load-worker-{assignment.worker_id}",
                )
                thread.start()
                workers.append(thread)

            while not self._stop_event.is_set() and any(thread.is_alive() for thread in workers):
                if time.monotonic() >= run_deadline:
                    self._stop_event.set()
                    break
                time.sleep(0.2)
        except Exception as exc:  # pragma: no cover - runtime safety
            with self._state_lock:
                self._last_error = str(exc)
            raise
        finally:
            self._stop_event.set()
            for thread in workers:
                thread.join(timeout=5)
            if self._client is not None:
                self._client.close()
            with self._state_lock:
                self._running = False
                self._finished_at = datetime.now(UTC)

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

    def _worker_loop(self, assignment: _WorkerAssignment, run_deadline: float) -> None:
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
        namespace_selector = NamespaceSelector(self._config.namespace, rng)
        operation_selector = OperationSelector(self._config.operation_mix, rng)
        generator = DocumentGenerator(self._config.schema, rng)
        assert self._client is not None
        executor = MongoOperationExecutor(
            client=self._client,
            namespace_selector=namespace_selector,
            operation_selector=operation_selector,
            document_generator=generator,
            id_registry=self._id_registry,
            rng=rng,
        )

        think_min = self._config.load.think_time_ms_min / 1000.0
        think_max = self._config.load.think_time_ms_max / 1000.0
        target_rate = self._config.load.target_ops_per_user_per_sec
        cycle_target_seconds = (1.0 / target_rate) if target_rate else 0.0

        while not self._stop_event.is_set() and time.monotonic() < run_deadline:
            cycle_start = time.perf_counter()
            for _ in range(assignment.simulated_users):
                if self._stop_event.is_set() or time.monotonic() >= run_deadline:
                    break
                result = executor.execute_once()
                self._metrics.record(result)
                if think_max > 0:
                    time.sleep(rng.uniform(think_min, think_max))

            if cycle_target_seconds > 0:
                cycle_elapsed = time.perf_counter() - cycle_start
                if cycle_elapsed < cycle_target_seconds:
                    time.sleep(cycle_target_seconds - cycle_elapsed)

    def _seed_initial_documents(self, client: MongoClient) -> None:
        seed_count = self._config.load.initial_seed_per_collection
        if seed_count <= 0:
            return

        rng = random.Random(self._config.random_seed)
        namespace_selector = NamespaceSelector(self._config.namespace, rng)
        generator = DocumentGenerator(self._config.schema, rng)
        batch_size = 500

        for target in namespace_selector.targets:
            remaining = seed_count
            collection = client[target.database][target.collection]
            while remaining > 0 and not self._stop_event.is_set():
                current_batch_size = min(batch_size, remaining)
                docs = [generator.generate_document() for _ in range(current_batch_size)]
                insert_result = collection.insert_many(docs, ordered=False)
                for inserted_id in insert_result.inserted_ids:
                    self._id_registry.add_id(target.database, target.collection, inserted_id)
                remaining -= current_batch_size
