# Code Review: mongo_load_tester/engine

Reviewer: Automated Code Review
Date: 2026-02-24

---

## 1. `engine/runner.py` — Load Test Lifecycle & Worker Orchestration

### Bug: `assert` used for runtime validation (line 561)

```python
assert self._client is not None
```

`assert` statements are stripped when Python runs with the `-O` (optimize) flag. This would silently allow `None` to be dereferenced, crashing the worker with an opaque `AttributeError`.

**Recommendation:** Replace with an explicit guard:

```python
if self._client is None:
    raise RuntimeError("MongoClient unavailable; cannot start worker.")
```

### Bug: Reading shared state without holding the lock (lines 432–438)

```python
self._id_registry = DocumentIdRegistry()                    # line 430, outside lock
self._metrics.record_active_users(0, target_users=0, ...)   # line 431
self._logger.info(
    "Load test starting max_users=%d target_users=%d ...",
    self._max_virtual_users,      # <-- no lock held
    self._target_virtual_users,   # <-- no lock held
    self._target_worker_threads,  # <-- no lock held
    self._target_duration_seconds,
    self._config.load.ramp_up_seconds,
)
```

These instance variables are protected by `_state_lock` everywhere else, but read here without the lock. A concurrent call to `apply_config` or `update_target_virtual_users` could produce a torn read.

**Recommendation:** Either read the values inside the lock block above and pass them to the logger, or move the logger call inside the lock.

### Bug: Registry replacement is raceable (line 430)

```python
self._id_registry = DocumentIdRegistry()
```

This assignment happens outside `_state_lock`. Daemon worker threads from a prior run could still be referencing the *old* `_id_registry` via their `executor` objects, while the new run creates a new, empty one. Although old workers should be stopping (stop event was set), there is a brief window where the old workers could insert IDs into the newly-created registry, or where new workers see a registry that an old worker is still mutating.

**Recommendation:** Move the assignment inside the lock block, and ensure old threads have fully joined before reassigning.

### Performance: Fixed 200ms polling interval in the main loop (line 474)

```python
time.sleep(0.2)
```

The main orchestration loop sleeps 200ms on every pass, meaning stop-event detection, duration checks, and worker pool reconciliation can all be delayed by up to 200ms. For a load testing tool that aims for precise timing, this adds non-trivial latency to control-plane responsiveness.

**Recommendation:** Use `self._stop_event.wait(timeout=0.2)` instead of `time.sleep(0.2)`. This returns immediately when the stop event is set, giving faster shutdown without changing the polling cadence when idle.

### Performance: Excessive lock churn while paused (lines 688–691)

```python
def _wait_if_paused(self) -> None:
    while self._pause_event.is_set() and not self._stop_event.is_set():
        self._sample_active_users()   # acquires _state_lock + metrics lock
        time.sleep(0.1)
```

While paused, every worker thread calls `_sample_active_users` every 100ms. With, say, 80 workers, this is ~800 lock acquisitions/sec on `_state_lock` — all to report the same "0 active users" value.

**Recommendation:** Only sample once per pause-check cycle by having the main loop handle user sampling while paused, or use a counter/flag so only one worker thread performs the sampling.

### Thread Safety: `_current_operation_mix` returns a mutable reference (line 736–738)

```python
def _current_operation_mix(self) -> OperationMixConfig:
    with self._state_lock:
        return self._operation_mix
```

This returns the live reference to `self._operation_mix`. The `OperationSelector` that receives it could call `.normalized()` on it while `update_operation_mix` replaces the reference. Since `OperationMixConfig` is a Pydantic model (immutable-ish once validated), the risk is low, but the pattern is fragile.

**Recommendation:** Return `self._operation_mix.model_copy()` so the caller has an independent snapshot.

### Code Quality: Falsy check instead of `None` check (line 728)

```python
cycle_target_seconds = (1.0 / target_rate) if target_rate else 0.0
```

`target_rate` is `float | None`. This relies on `None` being falsy, but it also silently treats `0.0` as "no rate limit" — even though `0.0` would be an invalid rate and should probably raise. The config validator prevents `0.0`, but defensive coding should use `is not None`.

**Recommendation:**

```python
cycle_target_seconds = (1.0 / target_rate) if target_rate is not None else 0.0
```

---

## 2. `engine/executor.py` — MongoDB Operation Executor

### Bug: Delete succeeds but doesn't verify actual deletion (lines 134–142)

```python
def _run_delete(self, collection, database, coll):
    document_id = self._choose_known_id(collection, database, coll)
    ...
    collection.delete_one({"_id": document_id})
    self._id_registry.remove_id(database, coll, document_id)
```

`delete_one` is called, and the ID is removed from the registry unconditionally. But `delete_one` might delete 0 documents (another thread raced and deleted the same ID first). The operation is recorded as "success" even though nothing was actually deleted. For a load testing tool, this inflates success metrics.

**Recommendation:** Check `result.deleted_count` and handle the zero-delete case (e.g., don't remove from registry, or record a different metric category).

### Bug: Race condition on shared IDs between threads (lines 134–142, 126–132)

Multiple workers can call `get_random_id` concurrently and receive the same `document_id`. For deletes, both threads will try `delete_one` with the same `_id`: the first succeeds, the second silently deletes nothing. For updates, both threads will update the same document, which may be intentional but could cause unexpected contention patterns in load testing results.

**Recommendation:** Consider using `pop_random_id` for delete operations (atomically remove from registry before issuing the delete), or document that duplicate targeting is expected behavior.

### Performance: New `RetryController` allocated per operation (lines 59–63)

```python
retry_controller = RetryController(
    policy=self._retry_policy_provider(),
    rng=self._rng,
    log_warning=self._logger.warning,
)
```

A new `RetryController` is constructed on every `execute_once()` call. Since the controller is stateless beyond its constructor arguments, this is wasted allocation, especially at high throughput (e.g., 10K+ ops/sec).

**Recommendation:** Cache the controller and only rebuild it when the policy changes (check by identity or version).

### Performance: `find_one({})` fallback is a full collection scan risk (lines 117, 137, 148)

When the ID registry has no known IDs (e.g., after heavy deletes), the code falls back to `collection.find_one({})`. On unindexed collections or large datasets, this can trigger a collection scan, which is extremely slow and creates artificial latency spikes in load test results.

**Recommendation:** Use `find_one({})` with a `hint={"_id": 1}` to force use of the default `_id` index, or use `aggregate([{"$sample": {"size": 1}}])` for truly random selection.

### Code Quality: `_run_update` silently becomes an insert (lines 126–132)

```python
def _run_update(self, collection, database, coll):
    ...
    if document_id is None:
        document_id = ObjectId()
    collection.update_one({"_id": document_id}, {"$set": update_fields}, upsert=True)
```

When no existing document is found, a brand-new `ObjectId` is generated and `upsert=True` inserts a new document. This means an "update" operation becomes an insert, which is misleading for load test analysis. The operation is recorded as `OperationType.UPDATE` but no actual update occurred.

**Recommendation:** Either skip the operation and return a distinguishable result, or use `upsert=False` and handle the `matched_count == 0` case explicitly.

---

## 3. `engine/metrics.py` — Metrics Aggregation

### Performance (Critical): Sorting up to 500K samples under lock (line 140)

```python
def snapshot(self) -> MetricsSnapshot:
    with self._lock:
        ...
        latency_values = sorted(self._latency_samples)  # up to 500,000 elements
```

Sorting 500K floats is O(n log n), taking ~50–200ms on typical hardware. During this time, the lock is held, blocking **all** worker threads from recording new metrics. Under heavy load, this creates a periodic throughput cliff every time the dashboard requests a snapshot.

**Recommendation:** Use a streaming percentile data structure such as a t-digest or HDR histogram. Alternatively, maintain a pre-sorted structure (e.g., `sortedcontainers.SortedList`) so percentile queries are O(log n) without needing a full sort. As a quick fix, use reservoir sampling to cap the sorted set to ~10K representative samples.

### Performance: Per-bucket sort inside `snapshot()` while holding lock (line 168)

```python
timeline_p95 = _percentile(sorted(bucket.latencies), 95)
```

This is called for *every* second in the timeline window (up to 600 seconds). Each bucket's `latencies` list can contain thousands of entries. The total sorting work is O(W * K log K) where W is the window size and K is ops/second. At 10K ops/sec over 600 seconds, this is millions of sort operations while the lock is held.

**Recommendation:** Pre-compute p95 when adding to the bucket (using an online algorithm), or compute it lazily outside the lock using a snapshot of the data.

### Performance: Unbounded per-bucket latency list memory (line 107)

```python
bucket.latencies.append(result.latency_ms)
```

Each `_TimelineBucket` stores every individual latency value for that second. At 10K ops/sec, each bucket holds 10K floats (~80KB). Over the 600-second window, that's ~48MB of latency data just for the timeline, on top of the global `_latency_samples` deque.

**Recommendation:** Use reservoir sampling within each bucket (cap at, say, 1000 samples) or an online percentile algorithm.

### Code Quality: Duplicated timeline iteration logic (lines 149–181 vs 207–243)

`snapshot()` and `export_timeline_rows()` contain nearly identical loops over the merged timeline keys. Any change to the iteration logic must be updated in two places.

**Recommendation:** Extract a shared `_iterate_timeline_with_users()` generator method that yields `(second_epoch, bucket, active_users, target_users, paused)` tuples.

### Thread Safety: Long lock hold during `export_timeline_rows()` (lines 206–243)

Similar to `snapshot()`, this method holds the lock for the entire duration of timeline iteration, row construction, and list building. For large timelines, this blocks all metric recording.

**Recommendation:** Copy the required data structures under the lock, then release the lock and do the processing outside it.

---

## 4. `engine/retry.py` — Retry with Exponential Backoff

### Bug: Jitter can collapse delay to zero asymmetrically (lines 74–77)

```python
if self.jitter_ratio > 0:
    jitter_ms = base_delay_ms * self.jitter_ratio
    base_delay_ms += rng.uniform(-jitter_ms, jitter_ms)
return max(base_delay_ms, 0.0) / 1000.0
```

With `jitter_ratio=1.0`, the jitter range is `[-base, +base]`, meaning the delay can go to zero. The `max(..., 0.0)` clamp prevents negative delays but creates an asymmetric distribution: low delays are over-represented. For a `base_delay_ms` of 50 with `jitter_ratio=1.0`, the actual distribution is `[0, 100]` but truncated below zero, biasing toward faster retries than intended.

**Recommendation:** Use decorrelated jitter (AWS-recommended): `delay = min(max_backoff, rng.uniform(initial_backoff, previous_delay * 3))`, or use `rng.uniform(base_delay_ms * (1 - jitter_ratio), base_delay_ms * (1 + jitter_ratio))` to keep the range always positive.

### Code Quality: Log message is slightly misleading (lines 131–133)

```python
self._log_warning(
    "Retry scheduled for %s (attempt %d/%d, backoff=%.3fs): %s"
    % (operation_name, attempts + 1, self._policy.max_attempts, delay_seconds, exc)
)
```

The message says "attempt 2/5" meaning "scheduling attempt 2," but `delay_seconds` was computed using `backoff_seconds(attempts, rng)` where `attempts` is the number of failed attempts so far. This is correct but could confuse readers: "attempt 2/5, backoff=0.1s" — the backoff 0.1s is actually for the *delay before* attempt 2, not after it.

**Recommendation:** Rephrase to: `"Retry #%d/%d for %s after %.3fs backoff: %s"`.

### Design Note: `time.sleep` during retry blocks the thread (line 134)

```python
time.sleep(delay_seconds)
```

This is inherent to a synchronous retry design. With many concurrent failures (e.g., during a MongoDB failover), many threads will be sleeping simultaneously, reducing effective throughput. This is actually desirable for backoff, but worth documenting.

---

## 5. `engine/control.py` — Dashboard Control Facade

### Bug: `restart()` has a race between `stop()` and `start()` (lines 162–176)

```python
def restart(self) -> tuple[bool, str]:
    self.stop()
    thread = self._thread_ref()
    if thread is not None and thread.is_alive():
        thread.join(timeout=15)
    ...
    started = self.start()
```

There is no lock held across the stop-join-start sequence. A concurrent call to `start()` from another source (e.g., another API request) could begin a new run between `join()` and `self.start()`, causing the restart to return `False` ("another run is already active"). The method is not atomic.

**Recommendation:** Hold `self._lock` for the entire restart sequence, or add an `_is_restarting` flag to prevent concurrent restarts.

### Bug: `start()` resets metrics while old workers may still be recording (line 27)

```python
def start(self) -> bool:
    with self._lock:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._metrics_store.reset()    # <-- old daemon workers could still be writing
        thread = threading.Thread(target=self._runner.run, ...)
```

`start()` checks `self._thread.is_alive()`, but daemon worker threads spawned by the runner are separate from `self._thread` (the main runner thread). Even if the main runner thread has finished, daemon workers could still be alive for a brief window, recording into the metrics store that was just reset.

**Recommendation:** The runner's `run()` method should `join` all worker threads before returning (it does, with a 5s timeout). However, if a worker doesn't finish within 5s, it will be orphaned. Consider making this timeout longer or logging a warning.

### Code Quality: `update_runtime_logging` iterates handlers by class name (lines 111–115)

```python
for handler in self._logger.handlers:
    if handler.__class__.__name__ == "InMemoryLogHandler":
        handler.setLevel(logging.DEBUG)
    else:
        handler.setLevel(numeric_level)
```

Checking `handler.__class__.__name__` is fragile — it breaks if the class is renamed or subclassed, and doesn't work across module boundaries (two classes with the same name in different modules). It also silently does the wrong thing if a new handler type is added.

**Recommendation:** Use `isinstance(handler, InMemoryLogHandler)` after importing the class, or tag the handler with a custom attribute.

---

## 6. `engine/client.py` — MongoClient Factory

### Security: Connection URI may contain plaintext credentials (line 24)

```python
return MongoClient(config.uri, **options)
```

The `config.uri` field can contain embedded credentials (e.g., `mongodb://admin:s3cret@host/db`). If the config object is ever serialized (to logs, to the metadata database, to the dashboard), credentials could be exposed. The URI is stored as a plain string in `MongoConnectionConfig` and in `AppConfig`, which is copied and returned by `snapshot_config()` and `current_config()`.

**Recommendation:**
1. Add a `masked_uri` property that redacts passwords from the URI for logging/display.
2. Consider using `pymongo.uri_parser.parse_uri` to validate the URI at config load time.
3. Ensure the config serialization path (dashboard, metadata export) uses the masked version.

### Code Quality: No input validation beyond config model

The function trusts that all config values are valid (they're validated by Pydantic). However, there's no validation that `min_pool_size <= max_pool_size`, which would cause a pymongo error at connection time rather than at config validation time.

**Recommendation:** Add a `@model_validator` to `MongoConnectionConfig` ensuring `min_pool_size <= max_pool_size`.

---

## 7. `engine/id_registry.py` — Document ID Cache

### Performance (Critical): `deque` is the wrong data structure (lines 16, 33, 42)

The `DocumentIdRegistry` uses `deque` for three operations:
- **`add_id`**: append + bounded trimming — O(1) ✓
- **`get_random_id`**: random index access (`bucket[rng.randrange(len(bucket))]`) — O(n) ✗ (CPython deque is a linked-list of blocks; indexed access is O(n))
- **`remove_id`**: value-based removal (`bucket.remove(document_id)`) — O(n) ✗

With `max_ids_per_namespace=20_000`, both `get_random_id` and `remove_id` are O(20K) in the worst case. Under high-throughput load testing, these are called on every read, update, and delete operation — potentially thousands of times per second per worker.

**Recommendation:** Replace `deque` with `list`. Lists have O(1) random access and O(n) removal, which is the same for removal but much better for random access. For truly O(1) removal, consider a combination of a `list` + `dict` mapping IDs to indices (swap-and-pop pattern):

```python
class _IndexedIdSet:
    def __init__(self, max_size: int):
        self._ids: list[Any] = []
        self._index: dict[Any, int] = {}
        self._max_size = max_size

    def add(self, doc_id: Any) -> None:
        if doc_id in self._index:
            return
        if len(self._ids) >= self._max_size:
            evicted = self._ids[0]
            self._swap_remove(0)
            del self._index[evicted]
        self._index[doc_id] = len(self._ids)
        self._ids.append(doc_id)

    def random(self, rng: random.Random) -> Any | None:
        if not self._ids:
            return None
        return self._ids[rng.randrange(len(self._ids))]

    def remove(self, doc_id: Any) -> None:
        idx = self._index.pop(doc_id, None)
        if idx is not None:
            self._swap_remove(idx)

    def _swap_remove(self, idx: int) -> None:
        last = len(self._ids) - 1
        if idx != last:
            self._ids[idx] = self._ids[last]
            self._index[self._ids[idx]] = idx
        self._ids.pop()
```

### Performance: Single global lock for all namespaces (line 17)

```python
self._lock = Lock()
```

One lock protects all namespaces. If the load test targets, say, 10 databases x 5 collections = 50 namespaces, all 80 worker threads contend on the same lock. Lock contention becomes a bottleneck.

**Recommendation:** Use per-namespace locks (a `defaultdict` of `Lock` objects), or use a `threading.RWLock` (via a third-party library or manual implementation) since reads vastly outnumber writes.

### Bug: `defaultdict(deque)` creates unbounded deques (line 16)

```python
self._ids: dict[tuple[str, str], deque[Any]] = defaultdict(deque)
```

The `deque` is created without `maxlen`. The manual bounding on lines 24–25 works correctly:

```python
while len(bucket) > self._max_ids_per_namespace:
    bucket.popleft()
```

But it's redundant — using `deque(maxlen=N)` would enforce the limit automatically and atomically. The current code also does `while` when `if` would suffice (only one element is appended before the check).

**Recommendation:** Use a factory that creates bounded deques:

```python
self._ids: dict[...] = defaultdict(lambda: deque(maxlen=self._max_ids_per_namespace))
```

Then lines 24–25 become unnecessary. (Note: if switching to `list` per the recommendation above, this point is moot.)

### Thread Safety: `remove_id` can leave stale references

After `remove_id`, other threads that already retrieved the same ID via `get_random_id` still hold a reference to it. If they issue a read or update against that deleted document, the operation will fail or upsert unexpectedly. This is inherent to the eventually-consistent design but worth documenting.

---

## Summary of Highest-Priority Issues

| Priority | File | Issue |
|----------|------|-------|
| **Critical** | `metrics.py:140` | Sorting 500K samples under lock blocks all workers |
| **Critical** | `id_registry.py:33` | O(n) random access on `deque` in hot path |
| **High** | `executor.py:141` | Delete reports success without checking `deleted_count` |
| **High** | `runner.py:561` | `assert` in production code; stripped by `-O` |
| **High** | `metrics.py:107` | Unbounded per-bucket latency list memory growth |
| **High** | `id_registry.py:42` | O(n) `deque.remove()` in hot path |
| **Medium** | `runner.py:430` | Registry replacement outside lock; raceable with old workers |
| **Medium** | `runner.py:474` | Use `stop_event.wait()` instead of `time.sleep()` |
| **Medium** | `executor.py:130` | Update silently becomes insert; misleading metrics |
| **Medium** | `client.py:24` | URI may expose credentials in logs/serialization |
| **Medium** | `control.py:162` | `restart()` is not atomic; concurrent calls can race |
| **Low** | `retry.py:76` | Jitter distribution biased toward zero for high ratios |
| **Low** | `control.py:112` | Handler type check by `__class__.__name__` is fragile |
| **Low** | `metrics.py:149,207` | Duplicated timeline iteration logic |
