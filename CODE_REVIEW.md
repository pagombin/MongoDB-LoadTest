# Code Review: mongo_load_tester

Reviewer: Cloud Agent  
Date: 2026-02-24  
Branch: `cursor/code-base-review-65c8`

---

## 1. `mongo_load_tester/config.py`

### Bug: Missing cross-field validation between `min_pool_size` and `max_pool_size` (lines 17-18)

`MongoConnectionConfig` validates each pool size independently but never checks `min_pool_size <= max_pool_size`. PyMongo will raise at runtime with a confusing error if `min_pool_size > max_pool_size`.

**Recommendation:** Add a model validator:

```python
@model_validator(mode="after")
def validate_pool_sizes(self) -> "MongoConnectionConfig":
    if self.min_pool_size > self.max_pool_size:
        raise ValueError("min_pool_size must be <= max_pool_size")
    return self
```

### Validation gap: No namespace character validation (lines 34-35)

`database_prefix` and `collection_prefix` accept any string, including characters that are invalid in MongoDB names (e.g., `$`, null bytes, `/`, `\`, `.` for database names, `$` for collection names). An invalid prefix will cause runtime errors deep in the MongoDB driver.

**Recommendation:** Add a `field_validator` that rejects illegal characters:

```python
@field_validator("database_prefix", "collection_prefix")
@classmethod
def validate_namespace_chars(cls, value: str) -> str:
    forbidden = set('/\\. "$\x00')
    if any(c in forbidden for c in value):
        raise ValueError(f"Namespace contains forbidden characters: {forbidden}")
    if not value.strip():
        raise ValueError("Namespace prefix must be non-empty")
    return value
```

### Validation gap: `SchemaFieldConfig.choices` type safety (lines 50, 54-58)

When `choices` is set on a non-string field (e.g., `field_type="int"` with `choices=["a", "b"]`), the generated document will contain a string value where an integer is expected. The `validate_bounds` validator only checks `min_value <= max_value` but doesn't verify that `choices` values are compatible with `field_type`.

**Recommendation:** Add validation that `choices` is only used with `field_type="string"`, or validate that each choice can be coerced to the declared type.

### Validation gap: `length` applies to all field types (line 47)

The `length` field defaults to 16 and is validated `ge=1` for *every* field type, but it is only meaningful for `"string"`. A user setting `field_type="bool", length=100` gets no warning.

**Recommendation:** Either make `length` optional (`None` by default) and only require it for string fields, or document that it is ignored for non-string types.

### Code quality: MongoDB URI is not validated (line 15)

The `uri` field accepts any string. A malformed URI like `"not-a-uri"` will only fail when PyMongo tries to connect, producing a confusing error far from the config parsing step.

**Recommendation:** Add a basic format check (e.g., must start with `mongodb://` or `mongodb+srv://`).

### Minor: `validate_non_empty` silently normalizes whitespace (lines 191-197)

The validator strips whitespace and returns the stripped value. This means `"  my-db  "` silently becomes `"my-db"`. While defensible, it could mask user mistakes. Consider at least logging a warning if the value was modified.

---

## 2. `mongo_load_tester/models.py`

### Bug: `MetricsSnapshot` claims thread-safety but is mutable (line 53)

The docstring says "Thread-safe immutable view" but the dataclass uses `slots=True` without `frozen=True`. All fields are freely mutable after construction, violating the stated invariant. If any consumer mutates a snapshot while another thread reads it, this is a data race.

**Recommendation:** Add `frozen=True`:

```python
@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
```

This requires changing `operation_counts`, `operation_latency_avg_ms`, and `timeline` to be built before construction rather than mutated after. If the `timeline` list must be mutable during construction, build it first and pass it to the frozen dataclass.

### Minor: `timeline` is a required field without a default (line 72)

Unlike `op_counts` in `TimelinePoint` (which has `default_factory=dict`), `timeline` has no default. Every caller must supply it. If this is intentional, it is fine, but it is inconsistent with the neighboring optional fields.

### Minor: No validation on numeric fields

`latency_ms` (line 32), `total_ops` (line 43), `avg_latency_ms` (line 44) etc. are not validated for non-negativity. A negative latency would indicate a bug upstream but would propagate silently through the system.

---

## 3. `mongo_load_tester/cli.py`

### Bug: `run_id` not recorded as finished on exception in headless mode (lines 88-96)

If `runner.run()` on line 89 raises an unexpected exception, execution jumps to the outer `except Exception` on line 136. The run was already recorded as started on line 88 but `record_run_finished` is never called. The run remains in `"running"` status in MongoDB until the next startup reconciliation.

**Recommendation:** Wrap the headless run in a try/finally:

```python
run_id = metadata_store.record_run_started(config_version=config_record.version)
try:
    runner.run()
    snapshot = metrics.snapshot()
    run_status = "failed" if runner.last_error else "completed"
    metadata_store.record_run_finished(run_id=run_id, status=run_status, snapshot=snapshot, error=runner.last_error)
except Exception as exc:
    metadata_store.record_run_finished(run_id=run_id, status="failed", snapshot=metrics.snapshot(), error=str(exc))
    raise
```

### Bug: Dashboard mode never records run start/finish (lines 107-129)

In dashboard mode, `record_run_started` and `record_run_finished` are never called from the CLI. If the dashboard itself doesn't call them, there is no run metadata for dashboard-driven runs. Verify that the dashboard UI handles this; otherwise this is a metadata gap.

### Code quality: `assert` used for control flow in production (lines 62, 74)

`assert log_buffer is not None` and `assert metadata_store is not None` will be silently stripped if Python runs with `-O` (optimized mode). These are guarding real runtime invariants that should use explicit checks.

**Recommendation:** Replace with:

```python
if log_buffer is None:
    raise RuntimeError("log_buffer was not initialized during bootstrap")
```

### Code quality: CLI overrides bypass Pydantic validation (line 42)

`bootstrap_config.metadata.load_test_id = load_test_id` assigns directly to a Pydantic model field. Unless `validate_assignment=True` is set in the model config (it is not), the `validate_non_empty` field validator is bypassed. An empty string `""` passed via `--load-test-id ""` would be accepted.

**Recommendation:** Either set `validate_assignment=True` on `MetadataConfig`, or validate the CLI input before assignment:

```python
if load_test_id is not None:
    cleaned = load_test_id.strip()
    if not cleaned:
        raise typer.BadParameter("--load-test-id must be non-empty")
    bootstrap_config.metadata.load_test_id = cleaned
```

### Minor: `KeyboardInterrupt` handling inconsistency (lines 130-133)

`KeyboardInterrupt` during dashboard mode raises `typer.Exit(code=130)` but does not call `record_run_finished`. If a run was started through the dashboard, its metadata is left in `"running"` state.

---

## 4. `mongo_load_tester/observability.py`

### Bug: Handler file descriptor leak on reconfiguration (line 67)

`logger.handlers.clear()` removes handlers without closing them. `configure_logging` is called twice in `cli.py` (lines 43 and 56) when the metadata config differs from the bootstrap config. The first call's `RotatingFileHandler` is dropped from the handler list but its file descriptor remains open.

**Recommendation:** Close handlers before clearing:

```python
for handler in logger.handlers[:]:
    handler.close()
    logger.removeHandler(handler)
```

### Bug: `asyncio` exception handler is never installed (lines 137-141)

`asyncio.get_running_loop()` raises `RuntimeError` when no event loop is running. Since `configure_logging` is called from synchronous CLI code, this will always fall into the `except RuntimeError: return` path. The asyncio handler is dead code.

**Recommendation:** Either remove the asyncio handler setup, or defer it to when an event loop is actually started (e.g., as a callback registered via `loop.call_soon` in the dashboard setup).

### Security concern: Context dict logged directly (line 131)

`logger.critical("%s | context=%s", message, context)` logs the full asyncio error context dict. Depending on the application, this could contain connection URIs, credentials, or other sensitive data that should not appear in log files.

**Recommendation:** Sanitize or allowlist the keys logged from the context dict.

### Code quality: `issubclass` vs `is` for exception type check (line 119)

`args.exc_type is KeyboardInterrupt` uses identity comparison. If a subclass of `KeyboardInterrupt` is raised (rare but possible), it would not be caught by this check. Line 109 correctly uses `issubclass()` for the same purpose.

**Recommendation:** Use `issubclass(args.exc_type, KeyboardInterrupt)` for consistency.

### Minor: InMemoryLogHandler captures all DEBUG messages (line 94)

`memory_handler.setLevel(logging.DEBUG)` captures every log record regardless of the configured level. If the user configures level `WARNING`, DEBUG and INFO messages still accumulate in the buffer, consuming memory unnecessarily. The buffer's `tail()` method filters by `min_level`, so they are not displayed, but they still occupy space.

**Recommendation:** Set the memory handler level to the configured level, or document this as intentional (allows the dashboard to show lower-severity messages on demand).

---

## 5. `mongo_load_tester/workload/schema.py`

### Bug: `generate_update_fields` crashes on empty schema (line 24)

If `self._schema.fields` is an empty list, `self._rng.choice([])` raises `IndexError`. While `SchemaConfig` has a non-empty default, a user can explicitly configure `fields: []` and Pydantic will accept it.

**Recommendation:** Either validate `len(fields) >= 1` in `SchemaConfig`, or guard here:

```python
def generate_update_fields(self) -> dict[str, Any]:
    if not self._schema.fields:
        raise ValueError("Cannot generate update fields from an empty schema")
    field = self._rng.choice(self._schema.fields)
    return {field.name: self._generate_value(field)}
```

### Bug: `int()` truncation can invert min/max bounds (line 34)

`self._rng.randint(int(field.min_value), int(field.max_value))` truncates floats toward zero. If `min_value=1.9` and `max_value=1.1`, both become `1` and the call succeeds but produces a degenerate range. Worse, if `min_value=0.9` and `max_value=0.1`, they become `int(0)` and `int(0)` respectively, which is subtly wrong. If `min_value=2.5` and `max_value=1.5`, they become `2` and `1`, and `randint(2, 1)` raises `ValueError`.

**Recommendation:** Use `math.floor` / `math.ceil` explicitly, or validate at the config level that int fields have integer-valued bounds:

```python
if field.field_type == "int":
    lo, hi = int(field.min_value), int(field.max_value)
    if lo > hi:
        raise ValueError(f"Integer field '{field.name}' has min_value > max_value after truncation")
    return self._rng.randint(lo, hi)
```

### Code quality: `choices` ignores `field_type` (line 29)

When `choices` is set, the method returns `self._rng.choice(field.choices)` regardless of `field_type`. If `field_type="int"` and `choices=["a","b"]`, the document gets a string where an integer is expected. MongoDB won't complain, but downstream query and aggregation logic may break.

**Recommendation:** Either restrict `choices` to string fields at the config level, or coerce choice values to the declared type.

### Performance: Character-by-character string generation (line 32)

```python
"".join(self._rng.choice(chars) for _ in range(field.length))
```

This generator expression calls `choice()` per character. For large `length` values in a hot loop this is slow.

**Recommendation:** Use batch selection:

```python
return "".join(self._rng.choices(chars, k=field.length))
```

`random.choices` with `k` is significantly faster for large values.

---

## 6. `mongo_load_tester/workload/selection.py`

### Performance: Weights rebuilt on every `choose()` call (lines 33-35)

When `mix_provider` is set, `_build_weights(self._mix_provider().normalized())` is called on every single operation selection. In a high-throughput load test doing thousands of ops/sec, this is a hot path. `normalized()` computes a division for each of four values, and `_build_weights` allocates a new list each time.

**Recommendation:** Cache the weights and only rebuild when the underlying mix changes. For example, store a version counter or compare against the previous mix values:

```python
def choose(self) -> OperationType:
    if self._mix_provider is not None:
        mix = self._mix_provider()
        normalized = mix.normalized()
        key = (normalized["read"], normalized["insert"], normalized["update"], normalized["delete"])
        if key != self._cached_key:
            self._weights = self._build_weights(normalized)
            self._cached_key = key
    return self._rng.choices(self._operations, weights=self._weights, k=1)[0]
```

### Minor: No guard against empty `_targets` (line 71)

`self._rng.choice(self._targets)` would raise `IndexError` if `_targets` is empty. Config validation (`ge=1` on `databases` and `collections_per_database`) prevents this in normal use, but a defensive check would make the failure mode clearer for callers who construct `NamespaceSelector` directly.

---

## 7. `mongo_load_tester/metadata/store.py`

### Bug: `record_run_finished` silently ignores missing runs (lines 180-197)

`update_one` does not check `result.matched_count`. If the `run_id` doesn't exist (e.g., due to a typo, race condition, or database reset), the update silently does nothing and the event is still appended, creating orphaned events.

**Recommendation:** Check the result:

```python
result = self._runs.update_one(...)
if result.matched_count == 0:
    self._logger.error("Attempted to finish unknown run_id=%s", run_id)
    return
```

### Bug: Corrupted config silently replaced with defaults (lines 237-238)

`_parse_config_document` falls back to `{}` if the `config` key is missing or falsy:

```python
payload = document.get("config") or {}
config = AppConfig.model_validate(payload)
```

An empty dict produces a fully default `AppConfig`. If the stored config was corrupted or accidentally deleted, this silently replaces it with defaults rather than raising an error. The caller has no way to distinguish "config was intentionally set to defaults" from "config was lost."

**Recommendation:** Raise an explicit error if the `config` key is missing:

```python
payload = document.get("config")
if payload is None:
    raise ValueError(f"Config document for load_test_id={document.get('load_test_id')} is missing 'config' field")
config = AppConfig.model_validate(payload)
```

### Code quality: `assert` in production code (line 124)

`assert updated is not None` guards against an impossible state (upsert + AFTER should always return a document), but `assert` is stripped by `-O`. Use an explicit check.

### Code quality: `updated_at` fallback masks data issues (lines 239-243)

If `updated_at` is not a `datetime` instance (e.g., stored as a string or epoch int), the code silently uses `datetime.now(UTC)`. This hides data integrity problems.

**Recommendation:** Log a warning when the fallback is used:

```python
if isinstance(updated_at_raw, datetime):
    updated_at = updated_at_raw
else:
    self._logger.warning("Config document has non-datetime updated_at=%r; using current time.", updated_at_raw)
    updated_at = datetime.now(UTC)
```

### Security: No input sanitization on `source` and `reason` (lines 106, 127-129)

`save_config` accepts arbitrary `source` and `reason` strings that are stored directly in MongoDB and appended to events. If these originate from user input (e.g., dashboard text fields), they could contain injection payloads for downstream consumers that render event logs as HTML.

**Recommendation:** Sanitize or length-limit these fields.

### Minor: `_ensure_indexes` in constructor can fail (line 53)

If the MongoDB user lacks index creation privileges, the constructor throws. This prevents the entire application from starting even if the indexes already exist.

**Recommendation:** Catch and log index creation failures, or use `try/except` with a warning.

### Minor: `run_id` index is not unique (line 234)

The `run_id` field uses `uuid4()` so collisions are astronomically unlikely, but the index is not declared as `unique=True`. For correctness, consider making it unique.

---

## 8. `mongo_load_tester/metadata/sync.py`

### Bug: TOCTOU race in `sync_once` (lines 60-78)

The method reads `_last_applied_version` under a lock (line 64-66), then releases the lock and does work (lines 67-78) including calling `mark_applied_version`. Between the version check and the `apply_config` call, another thread could also pass the version check and apply the same version twice.

**Recommendation:** Hold the lock for the entire sync operation, or use an atomic compare-and-swap pattern:

```python
def sync_once(self) -> None:
    record = self._metadata_store.fetch_config()
    if record is None:
        return
    with self._lock:
        if record.version <= self._last_applied_version:
            return
        if self._controller.is_running:
            return
        self._controller.apply_config(record.config, source="metadata-sync")
        self._last_applied_version = max(self._last_applied_version, record.version)
    # Event and log outside the lock to avoid holding it during I/O
    self._metadata_store.append_event(...)
    self._logger.info(...)
```

### Bug: TOCTOU race between `is_running` check and `apply_config` (lines 67-71)

The controller's running state is checked on line 67, but by line 71 the controller could have started a run. `apply_config` would then modify configuration mid-run, which contradicts the comment on line 69 ("defer... so static configuration updates are staged for next run").

**Recommendation:** The `apply_config` method on the controller should be made atomic with respect to run state, or `sync_once` should hold the lock as described above and the controller should expose an atomic `try_apply_config_if_idle` method.

### Minor: `stop()` does not verify thread termination (lines 52-58)

After `thread.join(timeout=timeout)`, the thread might still be alive if the timeout expired. The caller is not informed.

**Recommendation:** Check and log:

```python
def stop(self, timeout: float = 5.0) -> None:
    self._stop_event.set()
    with self._lock:
        thread = self._thread
    if thread is not None:
        thread.join(timeout=timeout)
        if thread.is_alive():
            self._logger.warning("Config sync thread did not stop within %.1fs timeout.", timeout)
```

### Minor: Redundant `max(int(...), 5)` (line 24)

`interval_seconds` is already typed as `int` and validated at `ge=5` in `MetadataConfig`. The defensive floor is harmless but obscures the trust boundary.

---

## Summary of Highest-Priority Issues

| # | File | Severity | Issue |
|---|------|----------|-------|
| 1 | `cli.py:88-96` | **High** | Run metadata left in "running" state on exception in headless mode |
| 2 | `observability.py:67` | **High** | File handler leak on logging reconfiguration |
| 3 | `store.py:180-197` | **High** | `record_run_finished` silently ignores non-existent runs |
| 4 | `store.py:237-238` | **High** | Corrupted config silently replaced with defaults |
| 5 | `sync.py:60-78` | **Medium** | TOCTOU race allows duplicate config application |
| 6 | `config.py:17-18` | **Medium** | Missing min/max pool size cross-validation |
| 7 | `schema.py:34` | **Medium** | `int()` truncation can invert min/max bounds |
| 8 | `cli.py:42` | **Medium** | CLI override bypasses Pydantic validation |
| 9 | `cli.py:62,74` | **Medium** | `assert` used for runtime invariants (stripped by `-O`) |
| 10 | `models.py:53` | **Medium** | `MetricsSnapshot` is mutable despite "immutable" contract |
| 11 | `schema.py:24` | **Medium** | `generate_update_fields` crashes on empty schema |
| 12 | `observability.py:119` | **Low** | `is` vs `issubclass` for exception type check |
| 13 | `selection.py:33-35` | **Low** | Weights rebuilt on every hot-path call |
| 14 | `observability.py:137-141` | **Low** | Asyncio exception handler is dead code |
| 15 | `store.py:53` | **Low** | Index creation failure blocks application startup |
