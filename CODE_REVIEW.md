# Code Review: Dashboard & Test Suite

## 1. `mongo_load_tester/dashboard/app.py`

### Bugs

**B1. Mutable `run_state` dict shared across callbacks without thread safety (line 116-123)**

`run_state` is a plain `dict` accessed and mutated by multiple Dash callbacks. Dash callbacks can fire concurrently (especially with `prevent_initial_call=True` and interval-driven updates). There is no lock or thread-safe container guarding reads and writes to `run_state`. A race between `refresh_dashboard` (reads `active_run_id`) and `control_runner` (writes `active_run_id`) could produce inconsistent state or lost updates.

**Recommendation:** Wrap `run_state` in a `threading.Lock` or use a thread-safe container. Alternatively, use Dash's server-side `dcc.Store` for state that must survive concurrent callbacks.

---

**B2. `_build_status_ui` checks `last_error` before `is_running` (line 2124-2133)**

If a runner finishes successfully and then a subsequent run fails, `last_error` may be stale. The status will show "FAILED" even if the runner is not currently running and the error came from a previous run. The logic should check `is_running` and `finished_at` first, and only consult `last_error` in the context of the most recent run.

**Recommendation:** Gate the error check on `runner.finished_at` and clear `last_error` when a new run starts.

---

**B3. `refresh_live_control_state_legacy` calls inner functions with `_` instead of the actual interval value (line 1334-1336)**

The function receives `_: int` (the interval value) and then calls `refresh_live_control_state(_)` and `refresh_capacity_chips(_)`. While `_` is a valid variable, the inner functions also accept `_: int` and ignore it, so this works by accident. If the inner functions' signatures ever change to use the parameter, this will break silently.

**Recommendation:** Pass `0` or an explicit sentinel instead of relying on name shadowing.

---

**B4. `staged_config = controller.current_config()` mutates the live config object in-place (lines 1619-1626)**

In `apply_static_config`, the code does `staged_config = controller.current_config()` and then directly mutates fields on it (e.g., `staged_config.namespace.database_prefix = ...`). If `current_config()` returns the actual live config object (not a deep copy), this mutation side-effects the running configuration immediately, bypassing the "staged for next run" semantics.

**Recommendation:** Use `controller.current_config().model_copy(deep=True)` to ensure isolation.

---

### Performance Issues

**P1. Massive callback return tuple (lines 1038-1135)**

The `refresh_dashboard` callback returns 27 output values on every refresh interval (default likely 1-2 seconds). This includes rebuilding 4 Plotly figures, an HTML table, and an insights panel every tick, even when no data has changed. For large timelines this is expensive.

**Recommendation:** Implement change detection: cache the last snapshot hash and skip figure rebuilds if unchanged. Alternatively, split the monolithic callback into independent callbacks with different refresh intervals (charts could update less frequently than KPIs).

---

**P2. `_throughput_trend` iterates the full timeline on every refresh (lines 2110-2121)**

The throughput trend computes averages over the entire (potentially very large) timeline list on every dashboard refresh.

**Recommendation:** Consider sampling or maintaining a rolling average instead of recomputing from scratch.

---

**P3. Six separate interval-driven callbacks all fire on the same `refresh` interval (lines 1038, 1148, 1204, 1216, 1228, 1269)**

All six callbacks trigger on `dcc.Interval("refresh")`. Some of them call `controller.live_settings_snapshot()` redundantly (e.g., `refresh_live_control_state` and `refresh_capacity_chips` both call it). This doubles the work.

**Recommendation:** Consolidate callbacks that query the same data, or use `dcc.Store` as an intermediate to avoid redundant computation.

---

### Security Concerns

**S1. No CSRF protection or authentication on control callbacks (lines 1347-1578)**

Start, stop, pause, resume, restart, and config mutation endpoints are all exposed via Dash callbacks with no authentication or CSRF tokens. Anyone with network access to the dashboard can control the load tester.

**Recommendation:** Add authentication middleware (e.g., Flask-Login) or restrict access via network controls. At minimum, add a confirmation dialog for destructive actions.

---

**S2. Schema JSON parsed from user input without size limits (line 2282-2301)**

`_parse_schema_config` accepts arbitrary JSON from the textarea. A user could submit an extremely large JSON payload, causing memory pressure or CPU exhaustion during parsing.

**Recommendation:** Add a size limit check before `json.loads()` (e.g., reject payloads > 64KB).

---

**S3. `_parse_non_empty_text` allows injection of arbitrary strings into namespace prefixes (line 2262-2266)**

Database and collection prefixes are accepted with only a whitespace check. MongoDB special characters (e.g., `$`, `.`, null bytes) in these values could cause unexpected behavior or errors.

**Recommendation:** Validate that namespace prefixes conform to MongoDB naming rules (no `$`, `.`, null bytes, etc.).

---

### UX Issues

**U1. No confirmation dialog for destructive actions (lines 1432-1442, 1469-1483)**

Stop and Restart buttons immediately execute without confirmation. A mis-click could terminate a long-running load test.

**Recommendation:** Add a `dcc.ConfirmDialog` component for Stop and Restart actions.

---

**U2. Error feedback from `_refresh_error_payload` replaces all KPI values with zeros (lines 2436-2475)**

When the refresh callback fails, all metrics are replaced with "0" and empty figures, losing the user's last known good state. This is disorienting.

**Recommendation:** Return `no_update` for most outputs and only update the status badge to show the error.

---

**U3. `_parse_optional_positive_float` silently returns `None` for negative values (line 2385-2386)**

If a user enters a negative target ops rate, it is silently treated as "unbounded" instead of showing a validation error.

**Recommendation:** Raise `ValueError` for negative values so the user gets explicit feedback.

---

### Code Quality

**Q1. 2484-line god function via `create_dashboard` (lines 104-1890)**

The entire dashboard is defined in a single function with nested callbacks, making it extremely difficult to test, navigate, or maintain. The function defines 8+ callbacks inline.

**Recommendation:** Extract callbacks into separate functions or classes. Consider a callback registry pattern.

---

**Q2. Style dictionaries are duplicated extensively (lines 31-100)**

Seven button style dictionaries differ only in border/background color. This violates DRY.

**Recommendation:** Create a `_button_style(border_color, bg_color, text_color="white")` factory function.

---

**Q3. Unused import: `replace` from `dataclasses` at module level (line 7)**

`replace` is used for `MetricsSnapshot` which is a `dataclass`, so it is used, but importing it alongside the Dash framework may confuse readers since `MetricsSnapshot` doesn't look like it's built here.

*(This is actually fine on closer inspection; noting for awareness.)*

---

**Q4. Magic numbers scattered throughout (e.g., lines 2086-2089)**

Thresholds for insights (1.0% error, 50ms P95, 2.0% retry) are hardcoded. These should be configurable.

**Recommendation:** Move insight thresholds to the config model.

---

---

## 2. `mongo_load_tester/dashboard/figures.py`

### Bugs

**B1. `_pause_spans` uses `prev` variable but never reads it meaningfully (line 277)**

The variable `prev` is assigned on every iteration but only used in the final `if` block (line 278). If the last point is paused, the span's end is computed as `prev.second + timedelta(seconds=1)`. However, since `prev` is the same as the last `point` in the loop, this is equivalent to `point.second + timedelta(seconds=1)`. The variable is confusing and could be replaced by the loop variable.

**Recommendation:** Remove the `prev` variable and use `snapshot.timeline[-1]` directly after the loop.

---

**B2. `build_operations_timeline` computes `write_counts` but does not include it as a meaningful label (line 28)**

The trace is labeled `write_ops` (line 57), which groups insert+update+delete. But the individual insert/update/delete traces are not shown, only total and read. Users cannot see the breakdown of write types in this chart.

**Recommendation:** Either add individual write-type traces (perhaps toggleable) or clearly document that this is a rolled-up write metric.

---

### Performance Issues

**P1. Four separate list comprehensions iterate `snapshot.timeline` redundantly (lines 23-29)**

`build_operations_timeline` iterates the timeline 7 times (x_values, read_counts, insert_counts, update_counts, delete_counts, write_counts, total_counts). A single pass would be more efficient for large timelines.

**Recommendation:** Use a single loop to build all arrays simultaneously:
```python
x, read, insert, update, delete, total = [], [], [], [], [], []
for p in snapshot.timeline:
    x.append(p.second)
    read.append(p.op_counts.get("read", 0))
    # etc.
```

---

**P2. Similar redundant iteration in `build_latency_timeline` and `build_user_ramp_timeline` (lines 93-95, 149-151)**

Each function makes 2-3 passes over the timeline.

**Recommendation:** Same single-pass approach.

---

### Code Quality

**Q1. Massive layout duplication across figure builders (lines 64-85, 120-141, 176-197)**

All three timeline builders set nearly identical `update_layout`, `update_xaxes`, and `update_yaxes` calls. Only `yaxis_title` and minor margin differences vary.

**Recommendation:** Extract a `_apply_timeline_layout(figure, yaxis_title, bottom_margin=50)` helper.

---

**Q2. Type annotation `tuple[object, object]` is too loose (lines 22, 92, 148, 265-266)**

The `x_range` parameter and pause span types use `tuple[object, object]` when they should be `tuple[datetime, datetime]`. This eliminates useful type checking.

**Recommendation:** Use `tuple[datetime, datetime]` or define a type alias.

---

**Q3. The `_add_pause_overlays` function adds potentially many `vrect` annotations (lines 250-262)**

For a long test with frequent pause/resume cycles, this could add hundreds of vertical rectangles to every figure, degrading rendering performance.

**Recommendation:** Limit to the N most recent pause overlays, or only show overlays visible within the current x_range.

---

---

## 3. `mongo_load_tester/dashboard/export.py`

### Bugs

**B1. `build_csv_bytes` uses the first row's keys as fieldnames, ignoring rows with extra keys (lines 21-22)**

If timeline rows have inconsistent keys (e.g., a later row has a key not present in the first row), `DictWriter` will silently drop those fields. This can happen if the schema evolves during a run.

**Recommendation:** Compute the union of all row keys:
```python
fieldnames = list(dict.fromkeys(k for row in rows for k in row.keys()))
```

---

**B2. `build_pdf_report` can place charts with negative Y coordinates (lines 101-102)**

`first_chart_y` and `second_chart_y` use `max(120, ...)` and `max(20, ...)` respectively, but if the summary text is very long (many summary lines), `text_y` could push `first_chart_y` down and `second_chart_y` could overlap with the bottom of the page or even go off-page. The chart positioning doesn't account for varying content height dynamically.

**Recommendation:** Implement proper page-break logic: if the charts don't fit on the first page after the summary, start them on a new page.

---

### Performance Issues

**P1. `build_pdf_report` generates three PNG images in memory simultaneously (lines 94-96)**

For high-resolution exports (scale=2), each image can be several megabytes. All three are kept in memory at once.

**Recommendation:** Generate and consume each image one at a time, releasing the bytes after drawing:
```python
ops_png = build_png_bytes(ops_figure, image_width, image_height)
ops_img = ImageReader(io.BytesIO(ops_png))
pdf_canvas.drawImage(ops_img, ...)
del ops_png, ops_img  # Allow GC
```

---

### Security Concerns

**S1. PDF generation uses `reportlab` with no input sanitization on summary text (lines 73-86)**

The summary lines include `snapshot.avg_latency_ms`, `snapshot.p99_latency_ms`, etc., which are numeric and safe. However, if `runner.last_error` (or other string fields) were included in the future, they could contain malicious content. This is low-risk currently but worth noting.

---

**S2. `build_png_bytes` calls `figure.to_image()` which invokes Kaleido/Orca subprocess (line 50)**

If the figure object is constructed from user-controlled data (it comes from client-side figure payloads in `download_png`), a carefully crafted payload could cause excessive CPU/memory usage in the rendering subprocess.

**Recommendation:** Add timeout and memory limits to the image generation, or validate figure complexity before rendering.

---

### Code Quality

**Q1. `build_pdf_report` has no pagination logic (lines 53-113)**

The function assumes all content fits on exactly two pages. If the summary grows or chart dimensions change, the layout breaks without warning.

**Recommendation:** Implement dynamic page-break logic based on remaining page height.

---

**Q2. Magic numbers for layout positions (lines 69, 71, 87, 92-93, 101-102)**

Values like `30`, `48`, `66`, `14` for spacing are hardcoded throughout. Any change requires updating multiple coordinated values.

**Recommendation:** Define layout constants (e.g., `MARGIN_LEFT = 30`, `LINE_HEIGHT = 14`, `HEADER_Y_OFFSET = 30`).

---

---

## 4. Test Suite Assessment

### File: `tests/test_dashboard_helpers.py`

**Coverage Gaps:**
- No tests for `_parse_positive_int`, `_parse_optional_positive_float`, `_parse_non_empty_text`, or `_parse_log_level` helpers.
- No tests for `_filter_snapshot_to_run_start`, `_timeline_x_range`, `_build_combined_timeline_export_figure`, `_build_status_ui`, or `_build_runtime_ui`.
- No tests for the main `create_dashboard` function or any of the Dash callbacks.
- No tests for the export functions (`build_csv_bytes`, `build_png_bytes`, `build_pdf_report`).
- No tests for figure builders (`build_operations_timeline`, etc.).

**Missing Edge Cases:**
- `_format_duration` is not tested with 0 or negative values.
- `_filter_snapshot_timeline` is not tested with an empty timeline.
- `_remove_incomplete_live_second` is not tested when `is_running=False`.
- `_compute_stable_ops_per_sec` only tests boundary cases; no large-value test.
- `_validate_think_time_values` doesn't test negative values or `None` inputs.
- `_schema_config_to_json` round-trip test doesn't verify field types are preserved.

**Test Isolation:**
- `test_remove_incomplete_live_second_filters_latest_partial_point` (line 83) depends on `datetime.now(UTC)` with `microsecond=500_000`. If the test runs at exactly a second boundary, the microsecond replacement to 500_000 could produce flaky results. **Risk: intermittent false negative.**

**False Positive Risk:**
- `test_filter_snapshot_timeline_applies_window` (line 62) creates points 120s and 30s in the past, then filters with a 60s window. This relies on `datetime.now(UTC)` being stable between fixture creation and function execution. Under extreme system load, clock drift could cause the test to pass incorrectly or fail. **Risk: low but present.**

---

### File: `tests/test_runner_safety.py`

**Coverage:** Tests only one scenario (connection failure). Missing:
- Tests for other types of top-level exceptions (e.g., `KeyboardInterrupt`, `SystemExit`).
- No test for partial failure mid-run.
- No test for concurrent `run()` calls.

**Test Quality:** Good. The test clearly validates the error capture mechanism and state transitions.

---

### File: `tests/test_retry.py`

**Coverage:** Good coverage of the retry controller with three scenarios: transient success, non-retryable failure, and exhaustion.

**Missing Edge Cases:**
- No test for jitter behavior (all tests set `jitter_ratio=0.0`).
- No test for `max_backoff_ms` capping behavior (the test values are too small to trigger it).
- No test for concurrent retry operations.
- No test for the case where `max_attempts=1` (no retries at all).

**Test Quality:** The use of `monkeypatch` on `time.sleep` is good for deterministic testing. The `lambda: (_ for _ in ()).throw(ValueError("bad input"))` pattern (line 53) is clever but obscure; a simple wrapper function would be more readable.

---

### File: `tests/test_observability.py`

**Coverage:** Tests log level filtering and tail limits.

**Missing Edge Cases:**
- No test for `ui_buffer_lines` limit enforcement (what happens when the buffer is full?).
- No test for thread safety of the log buffer.
- No test for empty buffer behavior.
- No test for `min_level=logging.DEBUG` (the lowest level).

**Test Isolation Concern:**
- `configure_logging` modifies global logger state (adds handlers). If tests run in parallel, they could interfere with each other's log output. Each test creates a new logger+buffer, but the underlying logging configuration is global. **Risk: flaky tests in parallel execution.**

---

### File: `tests/test_selection.py`

**Coverage:** Good basic coverage of operation and namespace selectors.

**Missing Edge Cases:**
- No test for equal weights across all operations.
- No test for very large namespace configurations (e.g., 100 databases x 100 collections).
- `test_operation_selector_uses_live_mix_provider` (line 37) tests the dynamic mix swap but doesn't test what happens if the provider raises an exception.

---

### File: `tests/test_runner_controls.py`

**Coverage:** Good coverage of control methods and sanitization.

**Test Isolation Concern:**
- `test_runner_active_elapsed_freezes_after_finalization_marker` (line 35) and `test_worker_thread_updates_expand_pool_when_running` (line 93) both directly set private attributes (`_run_started_monotonic`, `_final_active_elapsed_seconds`, `_running`, `_max_virtual_users`, etc.) via `# type: ignore`. This creates tight coupling to internal implementation details. If the runner's internals are refactored, these tests will break even if the public API remains stable.

**Recommendation:** Add public test helpers or use the public API to put the runner in the desired state.

---

### File: `tests/test_metrics_store.py`

**Coverage:** Tests snapshot calculations and reset behavior.

**Missing Edge Cases:**
- No test for concurrent `record()` calls (thread safety).
- No test for `history_window_seconds` eviction behavior (recording points beyond the window).
- No test for very large numbers of operations.
- No test for edge case where all operations are errors.
- `record_active_users` is called with `target_users` on line 26 but without it on line 61 — no test verifies the default behavior.

---

### File: `tests/test_config.py`

**Coverage:** Minimal — tests normalization, invalid mix rejection, and metadata defaults.

**Missing Edge Cases:**
- No test for YAML loading with missing fields (partial config).
- No test for environment variable overrides.
- No test for negative values in `LoadConfig`.
- No test for `SchemaConfig` validation.
- No test for `MongoConnectionConfig` URI validation or TLS option combinations.

---

### File: `tests/test_metadata_sync.py`

**Coverage:** Good coverage of sync-once logic with version comparison and running-state deferral.

**Missing Edge Cases:**
- No test for network failures during `fetch_config()`.
- No test for concurrent sync operations.
- No test for downgrade protection (version goes backward).

**Test Quality:** The use of `_FakeStore` and `_FakeController` dataclasses is clean and well-structured.

---

### File: `tests/test_client_build.py`

**Coverage:** Tests TLS behavior when unset and explicitly set to False.

**Missing Edge Cases:**
- No test for `tls=True`.
- No test for invalid URIs.
- No test for connection options like `maxPoolSize`, `retryWrites`, etc.
- No test for authentication parameters.

---

## Summary of Critical Findings

| Severity | File | Issue |
|----------|------|-------|
| **High** | `app.py:116` | Thread-unsafe `run_state` dict shared across concurrent callbacks |
| **High** | `app.py:1619` | Possible mutation of live config object instead of a copy |
| **High** | `app.py:1347` | No authentication on control endpoints |
| **Medium** | `app.py:2385` | Silent swallowing of invalid input (negative ops rate) |
| **Medium** | `app.py:2436` | Error handler erases all metric state from UI |
| **Medium** | `export.py:21` | CSV fieldnames from first row only; inconsistent rows lose data |
| **Medium** | `export.py:101` | PDF chart positioning can go off-page |
| **Medium** | `figures.py:23-29` | 7x redundant iteration over timeline |
| **Low** | `app.py:31-100` | Duplicated style dictionaries |
| **Low** | `figures.py:265` | `tuple[object, object]` type too loose |
| **Low** | Tests | No dashboard integration tests, no export tests, no figure builder tests |
| **Low** | Tests | Several tests rely on `datetime.now()` making them timing-sensitive |
