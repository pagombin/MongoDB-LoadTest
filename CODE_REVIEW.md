# Code Review: MongoDB Load Tester

**Reviewer:** Automated Code Review  
**Date:** 2026-02-24  
**Scope:** Full codebase review

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Critical Issues](#critical-issues)
3. [High-Priority Issues](#high-priority-issues)
4. [Medium-Priority Issues](#medium-priority-issues)
5. [Low-Priority / Code Quality](#low-priority--code-quality)
6. [Architecture & Structural Recommendations](#architecture--structural-recommendations)
7. [Testing Gaps](#testing-gaps)
8. [Infrastructure & DevOps](#infrastructure--devops)

---

## Executive Summary

The MongoDB Load Tester is a well-structured, modular Python application with clear separation of concerns, strong typing via Pydantic, and a thoughtful layered architecture (CLI → engine → workload → dashboard → metadata). The codebase demonstrates good practices: consistent naming, thread safety awareness, and comprehensive configuration validation.

However, several issues need attention — ranging from performance bottlenecks that undermine the tool's accuracy under load, to thread safety races, silent data corruption paths, and significant gaps in test coverage and CI/CD infrastructure.

**Findings by severity:**

| Severity | Count |
|----------|-------|
| Critical | 4 |
| High | 10 |
| Medium | 14 |
| Low / Code Quality | 12 |

---

## Critical Issues

### C1. Sorting 500K latency samples under lock (`metrics.py:140`)

```python
def snapshot(self) -> MetricsSnapshot:
    with self._lock:
        ...
        latency_values = sorted(self._latency_samples)  # up to 500,000 elements
```

Sorting 500K floats is O(n log n), taking ~50–200ms. During this time, the lock is held, **blocking all worker threads** from recording new metrics. This creates a periodic throughput cliff every time the dashboard requests a snapshot.

Additionally, per-bucket sorts at line 168 compound this: `timeline_p95 = _percentile(sorted(bucket.latencies), 95)` is called for every second in the timeline window (up to 600 seconds), each with thousands of entries.

**Recommendation:** Use a streaming percentile data structure (t-digest or HDR histogram). As a quick fix, use reservoir sampling to cap sorted sets to ~10K representative samples. Pre-compute p95 per bucket on insertion using an online algorithm.

### C2. O(n) random access on `deque` in hot path (`id_registry.py:33`)

```python
bucket[rng.randrange(len(bucket))]
```

CPython's `deque` is implemented as a doubly-linked list of blocks. Random index access is O(n). With `max_ids_per_namespace=20,000`, this is called on every read, update, and delete operation — thousands of times per second per worker.

The `remove_id` call at line 42 (`bucket.remove(document_id)`) is also O(n).

**Recommendation:** Replace `deque` with a `list` + `dict` swap-and-pop pattern for O(1) random access and O(1) removal:

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

### C3. Run metadata left in "running" state on crash (`cli.py:88-96`)

If `runner.run()` throws, execution jumps to the outer `except` block. `record_run_finished` is never called, leaving the run stuck as `"running"` in MongoDB until the next startup reconciliation.

**Recommendation:** Wrap the headless run path in `try/finally`:

```python
try:
    runner.run()
finally:
    meta_store.record_run_finished(run_id, runner.snapshot_config())
```

### C4. Dashboard shared state has no thread safety (`dashboard/app.py:116-123`)

The `run_state` dictionary is shared across concurrent Dash callbacks with no lock or synchronization. Dash callbacks can run concurrently (via `threading` or `asyncio`), meaning two callbacks can read and write `run_state` simultaneously, causing torn reads or lost updates.

**Recommendation:** Use `threading.Lock` to guard access to `run_state`, or restructure it as a `dcc.Store` component backed by the browser session.

---

## High-Priority Issues

### H1. File handler leak on logging reconfiguration (`observability.py:67`)

`logger.handlers.clear()` drops references to `RotatingFileHandler` without calling `.close()`. `configure_logging` is called twice in `cli.py` (lines 43 and 56) when the metadata config differs, leaking the previous file handler's file descriptor.

**Recommendation:** Close handlers before clearing:

```python
for handler in logger.handlers[:]:
    handler.close()
    logger.removeHandler(handler)
```

### H2. `record_run_finished` silently ignores missing runs (`store.py:180-197`)

`update_one` does not check `matched_count`. If the `run_id` doesn't exist (e.g., due to a database wipe), the update is silently lost while an orphaned event is still appended.

**Recommendation:** Check `result.matched_count` and log a warning or raise if the run document was not found.

### H3. Corrupted config silently replaced with defaults (`store.py:237-238`)

```python
document.get("config") or {}
```

If the `config` field is missing or `None`, this quietly produces a default `AppConfig`, masking data corruption. The operator sees valid-looking defaults instead of an error.

**Recommendation:** Raise a `ValueError` or log a warning if the stored config is missing or empty.

### H4. `assert` used for runtime invariants (`cli.py:62,74`; `runner.py:561`)

`assert` statements are stripped when Python runs with `-O`. Several critical invariants use `assert`:
- `cli.py:62`: `assert log_buffer is not None`
- `runner.py:561`: `assert self._client is not None`

**Recommendation:** Replace with explicit `if/raise RuntimeError(...)` guards.

### H5. Delete reports success without checking `deleted_count` (`executor.py:134-142`)

```python
collection.delete_one({"_id": document_id})
self._id_registry.remove_id(database, coll, document_id)
```

`deleted_count` is never checked. Another thread may have already deleted the same document. The operation is recorded as "success" even though nothing was deleted, inflating success metrics.

**Recommendation:** Check `result.deleted_count` and handle the zero-delete case appropriately.

### H6. Update silently becomes an insert (`executor.py:126-132`)

When no existing document is found, a brand-new `ObjectId` is generated and `upsert=True` inserts a new document. The operation is recorded as `OperationType.UPDATE` but no actual update occurred — misleading for load test analysis.

**Recommendation:** Use `upsert=False` and handle `matched_count == 0` explicitly, or record the actual operation type.

### H7. `schema.py` `int()` truncation can invert min/max (line 34)

If configured with float bounds like `min: 0.9, max: 1.1`, `int()` truncation produces `randint(0, 1)` which is fine, but `min: 1.9, max: 1.1` would produce `randint(1, 1)`. More dangerously, negative floats could cause `int(-0.5)=0` vs `int(0.3)=0`, collapsing the range.

**Recommendation:** Use `math.floor` / `math.ceil` explicitly and validate `min <= max` after truncation.

### H8. Empty fields list causes `IndexError` (`schema.py:24`)

`generate_update_fields` calls `rng.choice([])` when `fields: []` is configured, raising `IndexError`.

**Recommendation:** Guard with an early return or validate at config time that fields is non-empty.

### H9. No authentication or access control on dashboard (`dashboard/app.py`)

The dashboard exposes control callbacks (start/stop/pause/resume/restart) with no authentication. Anyone with network access to the dashboard can control the load tester.

**Recommendation:** Add basic authentication (e.g., HTTP Basic Auth via Dash's `auth` or a middleware), or bind to `localhost` by default with a flag to enable external access.

### H10. Dashboard control mutation of live config (`dashboard/app.py:1619-1626`)

`controller.current_config()` may return the live config object, and the code mutates it in-place, potentially side-effecting the running configuration.

**Recommendation:** Use `.model_copy(deep=True)` before mutating the config for staged changes.

---

## Medium-Priority Issues

### M1. TOCTOU race in config sync (`sync.py:60-78`)

The version check and `apply_config` are not atomic. The same config version could be applied twice if two sync cycles overlap.

**Recommendation:** Use a compare-and-swap pattern or hold a lock across the check-and-apply.

### M2. No cross-validation of pool sizes (`config.py:17-18`)

No validation that `min_pool_size <= max_pool_size`. PyMongo will throw at runtime with a confusing error instead of a clear config validation error.

**Recommendation:** Add a `@model_validator` to `MongoConnectionConfig`.

### M3. `validate_non_empty` bypassed on assignment (`cli.py:42`)

```python
bootstrap_config.metadata.load_test_id = load_test_id
```

This bypasses the Pydantic `validate_non_empty` validator because `validate_assignment` is not enabled on the model.

**Recommendation:** Enable `model_config = ConfigDict(validate_assignment=True)` on the relevant model.

### M4. Registry replacement outside lock (`runner.py:430`)

`self._id_registry = DocumentIdRegistry()` is assigned outside `_state_lock`. Old daemon workers could still reference the old registry.

**Recommendation:** Move inside the lock block and ensure old threads have joined.

### M5. `time.sleep` instead of `stop_event.wait` (`runner.py:474`)

```python
time.sleep(0.2)
```

This delays stop-event detection by up to 200ms. Using `self._stop_event.wait(timeout=0.2)` gives instant response to stop signals.

### M6. Excessive lock churn while paused (`runner.py:688-691`)

All paused workers call `_sample_active_users()` every 100ms. With 80 workers, that's ~800 lock acquisitions/sec — all reporting the same "0 active users."

**Recommendation:** Designate a single thread (the main loop) to sample users while paused.

### M7. `restart()` is not atomic (`control.py:162-176`)

No lock held across the stop-join-start sequence. Concurrent calls can race.

**Recommendation:** Hold `self._lock` for the entire restart sequence.

### M8. Weights rebuilt on every `choose()` call (`selection.py:33-35`)

When `mix_provider` is set, operation weights are rebuilt on every call. This is a hot path with thousands of calls per second.

**Recommendation:** Cache the weights and invalidate only when the mix changes (e.g., by version number).

### M9. URI may expose credentials (`client.py:24`)

The `config.uri` field can contain embedded credentials that may leak through config serialization, logging, or the metadata store.

**Recommendation:** Add a `masked_uri` property that redacts passwords for display/logging purposes.

### M10. `_ensure_indexes()` blocks startup (`store.py:53`)

Index creation in the constructor blocks application startup. If the MongoDB user lacks `createIndex` privileges, the entire application fails to start.

**Recommendation:** Make index creation lazy or wrap it in a try/except with a warning.

### M11. Hardcoded collection names (`store.py:50-52`)

Collection names are hardcoded strings scattered across the store.

**Recommendation:** Extract to class-level constants.

### M12. CSV fieldnames from first row only (`dashboard/export.py:21-22`)

CSV fieldnames are derived from the first row. Later rows with extra keys silently lose data.

**Recommendation:** Collect all unique keys across all rows, or define a fixed schema.

### M13. Dashboard error handler erases all KPI values (`dashboard/app.py:2436-2475`)

The error handler sets all KPI values to zeros, losing the user's last known good state.

**Recommendation:** Return `dash.no_update` for outputs that should preserve their previous values on error.

### M14. `MetricsSnapshot` is mutable despite docstring (`models.py:53`)

Docstring says "Thread-safe immutable view" but the dataclass is not `frozen=True`.

**Recommendation:** Add `@dataclass(frozen=True)` or remove the misleading docstring.

---

## Low-Priority / Code Quality

### L1. `asyncio.get_running_loop()` is dead code (`observability.py:137-141`)

Always raises `RuntimeError` in synchronous CLI code. The asyncio exception handler is never actually installed.

### L2. `is` instead of `issubclass` for exception check (`observability.py:119`)

Uses `is` for `KeyboardInterrupt` check (inconsistent with line 109).

### L3. Handler type check by `__class__.__name__` (`control.py:112`)

Fragile string comparison. Use `isinstance(handler, InMemoryLogHandler)`.

### L4. Falsy check instead of `None` check (`runner.py:728`)

```python
cycle_target_seconds = (1.0 / target_rate) if target_rate else 0.0
```

Treats `0.0` and `None` identically. Should use `is not None`.

### L5. `RetryController` allocated per operation (`executor.py:59-63`)

A new `RetryController` is constructed on every `execute_once()` call. Since it's stateless, cache it and rebuild only when the policy changes.

### L6. `find_one({})` fallback risks collection scan (`executor.py:117,137,148`)

When the ID registry is empty, falls back to `collection.find_one({})`. On large collections, this may trigger a collection scan.

**Recommendation:** Add `hint={"_id": 1}` or use `$sample` aggregation.

### L7. Jitter distribution biased toward zero (`retry.py:74-77`)

With `jitter_ratio=1.0`, delay can go to zero. The `max(..., 0.0)` clamp creates an asymmetric distribution biased toward faster retries.

### L8. Duplicate timeline iteration logic (`metrics.py:149 vs 207`)

`snapshot()` and `export_timeline_rows()` contain nearly identical timeline loops.

**Recommendation:** Extract a shared `_iterate_timeline()` generator.

### L9. Duplicate `ReadMe.md` file

Both `README.md` and `ReadMe.md` exist at the project root. The latter just references the former — unnecessary.

### L10. Layout code duplicated across figure builders (`figures.py:64-85, 120-141, 176-197`)

All three timeline figure builders contain identical layout configuration.

**Recommendation:** Extract a shared `_timeline_layout()` helper.

### L11. Pause overlays added without limit (`figures.py:250-262`)

Frequent pause/resume cycles accumulate `vrect` overlays, potentially degrading rendering performance.

### L12. PDF chart positioning can overflow page (`export.py:101-102`)

No page-break logic exists. Long summary text pushes charts off-page.

---

## Architecture & Structural Recommendations

### A1. Split `dashboard/app.py` (2,484 lines)

This file is far too large. It contains app initialization, HTML layout, and all Dash callbacks in a single file.

**Recommendation:** Split into:
- `app.py` — App factory and initialization
- `layout.py` — HTML/Dash component layout
- `callbacks/controls.py` — Start/stop/pause callbacks
- `callbacks/metrics.py` — Metrics refresh callbacks
- `callbacks/config.py` — Configuration update callbacks
- `callbacks/export.py` — Export callbacks

### A2. Split `engine/runner.py` (819 lines)

The `LoadTestRunner` class handles orchestration, worker management, seeding, and config application.

**Recommendation:** Extract:
- `WorkerManager` — Worker lifecycle and thread pool management
- `SeedingService` — Initial document seeding
- `ConfigApplier` — Live config update logic

### A3. Introduce interfaces for dashboard-engine coupling

`dashboard/app.py` imports many engine modules directly. This tight coupling makes it difficult to test the dashboard in isolation.

**Recommendation:** Introduce a protocol/interface that the dashboard depends on, with the engine providing the implementation.

### A4. Consider per-namespace locks in ID registry

A single global lock for all namespaces creates contention. With 50 namespaces and 80 workers, this is a bottleneck.

**Recommendation:** Use per-namespace `Lock` objects.

---

## Testing Gaps

### Current coverage

| Module | Tested? | Notes |
|--------|---------|-------|
| `engine/runner.py` | Partial | Controls tested, worker lifecycle not tested |
| `engine/executor.py` | No | No tests for MongoDB operations |
| `engine/metrics.py` | Yes | Good coverage |
| `engine/retry.py` | Yes | Missing jitter/capping edge cases |
| `engine/control.py` | Indirect | Only through runner tests |
| `engine/client.py` | Yes | TLS configuration tested |
| `engine/id_registry.py` | No | No tests |
| `config.py` | Yes | Good coverage |
| `models.py` | No | No tests for data classes |
| `cli.py` | No | No CLI tests |
| `observability.py` | Yes | Good coverage |
| `workload/schema.py` | No | No tests — missing edge cases (empty fields, float bounds) |
| `workload/selection.py` | Yes | Good coverage |
| `metadata/store.py` | No | Only tested indirectly via sync |
| `metadata/sync.py` | Yes | Good coverage |
| `dashboard/app.py` | No | No callback or integration tests |
| `dashboard/figures.py` | No | No tests |
| `dashboard/export.py` | No | No tests |

### Recommended additions

1. **Integration test for CLI commands** — test `run`, `validate`, `init-config` with mock MongoDB
2. **Thread safety tests** — concurrent read/write tests for `MetricsStore`, `DocumentIdRegistry`, `InMemoryLogBuffer`
3. **Executor tests** — mock PyMongo to verify operation correctness, especially delete/update edge cases
4. **Dashboard callback tests** — use Dash testing utilities or mock the controller
5. **Export tests** — verify CSV/PNG/PDF output format and content
6. **Schema generation edge cases** — empty fields, float bounds, type mismatches
7. **Coverage reporting** — add `pytest-cov` to dev dependencies and track coverage

### Test quality concerns

- Several tests use `datetime.now(UTC)` making them timing-sensitive under load
- `test_observability.py` modifies global logger state, risking test interference in parallel execution
- `test_runner_controls.py` sets private `_`-prefixed attributes directly, creating tight coupling to internals

---

## Infrastructure & DevOps

### No CI/CD pipeline

There is no GitHub Actions workflow, no `.gitlab-ci.yml`, no automated testing on commits or PRs.

**Recommendation:** Add `.github/workflows/ci.yml`:

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
      - run: pip install -e ".[dev]"
      - run: pytest --tb=short -q
```

### No containerization

No Dockerfile exists. For a tool that tests MongoDB clusters, containerization would simplify deployment and reproducibility.

**Recommendation:** Add a `Dockerfile` and optionally a `docker-compose.yml` with a MongoDB instance for local development/testing.

### No coverage reporting

No visibility into what percentage of code is covered by tests.

**Recommendation:** Add `pytest-cov` to dev dependencies:

```toml
[project.optional-dependencies]
dev = ["pytest>=8.3.4", "pytest-cov>=4.1.0"]
```

### No `.env.example`

Configuration relies entirely on YAML files, but connection URIs with credentials are embedded in the config. There's no example environment file documenting expected variables.

### No type checking in CI

Despite good type annotations throughout, there's no `mypy` or `pyright` configuration to enforce type correctness.

**Recommendation:** Add `mypy` to dev dependencies and CI:

```toml
dev = ["pytest>=8.3.4", "mypy>=1.8.0"]
```

---

## Summary of Top Recommendations (Ordered by Impact)

1. **Replace latency percentile computation** with a streaming algorithm (t-digest/HDR) to eliminate the metrics lock bottleneck (C1)
2. **Replace `deque` with indexed list** in `DocumentIdRegistry` for O(1) random access (C2)
3. **Add `try/finally` around run lifecycle** to prevent stuck "running" metadata (C3)
4. **Add thread safety to dashboard `run_state`** (C4)
5. **Close file handlers before clearing** in `configure_logging` (H1)
6. **Check `deleted_count` / `matched_count`** in executor operations (H5, H6)
7. **Replace `assert` with explicit guards** throughout (H4)
8. **Add CI/CD pipeline** with automated testing (Infrastructure)
9. **Split `dashboard/app.py`** into multiple files (A1)
10. **Add tests for untested modules** — especially executor, dashboard, and schema (Testing)
