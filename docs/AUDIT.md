# Audit of MongoDB-LoadTest (pre-redesign)

Audited commit: `c55da08` (head of the default branch when this work started).
Audit date: 2026-10-23.

Everything below was found by reading the code and running it. Each finding points to a file and line in the audited commit, a command and its output, or a screenshot in `docs/design/before/`.

## 1. Inventory

### 1.1 Stack

| Area | What is used | Evidence |
|---|---|---|
| Language | Python, `requires-python >= 3.10` (code uses `datetime.UTC`, so 3.11 is the real minimum) | `pyproject.toml:9`, `mongo_load_tester/engine/runner.py:11` |
| Web UI | Plotly Dash single page, served by Waitress over plain HTTP | `mongo_load_tester/dashboard/app.py:99`, `mongo_load_tester/cli.py:179-188` |
| Charts | Plotly figures rebuilt server-side every refresh | `mongo_load_tester/dashboard/figures.py` |
| Live updates | `dcc.Interval` polling, one callback with 34 outputs every second | `app.py:1537`, `app.py:1704-1742` |
| Driver | PyMongo (sync), one shared `MongoClient` per run | `engine/client.py:10-24` |
| Config | YAML validated by Pydantic | `config.py:428-445` |
| CLI | Typer: `run`, `validate`, `init-config` | `cli.py:24`, `cli.py:410`, `cli.py:423` |
| Persistence | MongoDB: the target cluster's `loader_metadata` database | `metadata/store.py:11-16`, `config.py:390` |
| Exports | CSV (stdlib), PNG via Kaleido, PDF via ReportLab + Kaleido | `dashboard/export.py` |

### 1.2 Dependencies (as installed by `pip install -e .[dev]` on 2026-10-23)

Direct dependencies, with the installed version and the license from package metadata:

| Package | Version | License | Shipped at runtime |
|---|---|---|---|
| dash | 4.4.1 | MIT | yes |
| plotly | 7.1.0 | MIT | yes |
| kaleido | 1.5.0 | MIT | yes (needs Google Chrome, see 5.1) |
| faker | 40.43.0 | MIT | yes |
| pydantic / pydantic_core | 2.14.1 / 2.50.1 | MIT | yes |
| pymongo | 4.19.0 | Apache-2.0 | yes |
| dnspython | 2.9.0 | ISC | yes (SRV lookups) |
| pyyaml | 6.0.3 | MIT | yes |
| reportlab | 5.1.0 | BSD | yes |
| typer | 0.27.3 | MIT | yes |
| waitress | 3.0.2 | ZPL-2.1 | yes |
| pytest | 9.1.1 | MIT | dev only |

Transitive runtime packages and licenses: Flask (BSD-3), Werkzeug (BSD-3), Jinja2 (BSD), MarkupSafe (BSD-3), itsdangerous (BSD), click (BSD-3), blinker (MIT), requests (Apache-2.0), urllib3 (MIT), idna (BSD-3), charset-normalizer (MIT), certifi (MPL-2.0), pillow (MIT-CMU), rich (MIT), Pygments (BSD-2), markdown-it-py (MIT), mdurl (MIT), shellingham (ISC), choreographer (MIT), logistro (MIT), narwhals (MIT), nest-asyncio (BSD), retrying (Apache-2.0), simplejson (MIT or AFL-2.1), typing_extensions (PSF-2.0), importlib_metadata (Apache-2.0), zipp (MIT), packaging (Apache-2.0 or BSD-2), platformdirs (MIT), janus (Apache-2.0), comm (BSD), annotated-types (MIT), typing-inspection (MIT), annotated-doc (MIT).

Command used:

```
python -c "import importlib.metadata as m; ..."
```

The full output is reproduced in the table above. No dependency declares its versions as pinned; every requirement in `pyproject.toml:11-21` is a `>=` floor, so two installs on different days can differ.

### 1.3 Entry points and build

- Console script `mongo-load-tester = mongo_load_tester.cli:main` (`pyproject.toml:30`).
- Build: `pip install .` with setuptools. There is no lock file, no frontend build, no release packaging, no CI.
- Runtime layout: the working directory. The log file defaults to `artifacts/mongo-load-tester.log` relative to the current directory (`config.py:349`, `observability.py:82-84`); exports go to `artifacts/` (`config.py:336`).

### 1.4 Data storage

All state goes to the target cluster, in database `loader_metadata` (`metadata/store.py:11-16`): configs, runs, events, per-collection schemas, workload state, worker registry, and distributed live metrics. Running the original app against a local replica set and listing databases afterwards:

```
$ mongosh --eval 'db.adminCommand({listDatabases:1}).databases.map(d=>d.name).join(",")'
admin,config,loader_metadata,local,oltp_sim_1
```

Per-second metrics live only in process memory (`engine/metrics.py:157-160`) and are lost when the process exits.

### 1.5 User-facing features

1. Five operation types: read, insert, update, delete, findAndModify (`models.py:10-16`).
2. Document schema with 14 field types, nested objects and arrays, Faker providers, string composites, date modes (`config.py:52-131`, `workload/schema.py`).
3. Per-collection schema overrides stored in metadata (`workload/registry.py`, `metadata/store.py:342-371`).
4. Query targeting: filter fields, projections, sort, update operators, automatic indexes (`config.py:134-228`, `engine/executor.py`).
5. Namespace fan-out over N databases x M collections (`config.py:41-49`, `workload/selection.py:60-77`).
6. Closed-loop load with virtual users, worker threads, think time, per-user pacing, ramp-up (`config.py:280-312`).
7. Live controls: users, workers, think time, mix, retry policy, log level, duration (`app.py:2288-2541`).
8. Start, pause, resume, stop, restart (`app.py:2350-2440`).
9. Initial seeding per collection (`runner.py:806-865`).
10. Application-level retry with exponential backoff and jitter (`engine/retry.py`).
11. Distributed leader/follower mode across processes via the metadata database (`cli.py:217-379`, `engine/distributed.py`).
12. Dashboard with 10 KPIs, 11 charts, log viewer, smart insights, operator guide (`app.py`, `figures.py`).
13. Exports: CSV, PNG, PDF (`app.py:3292-3353`).
14. Schema Builder modal (`app.py:2816-3140`).
15. Run records with summary in metadata, stale "running" runs reconciled to "interrupted" at startup (`metadata/store.py:250-272`).

Screenshots of the original UI: `docs/design/before/01-idle-top.png`, `02-idle-full.png`, `03-running-top.png`, `04-running-full.png`, `05-stopped-top.png`.

## 2. How performance is measured today

### 2.1 Timing

The live load loop is `LoadTestRunner._worker_loop` (`runner.py:597-804`). It does not use `MongoOperationExecutor.execute_once`; the executor is constructed (`runner.py:628-640`) but never called from the loop. Each "cycle" a worker:

1. Splits its `active_users` count across insert, read, update, delete by the mix (`runner.py:665-671`).
2. Sends one `insert_many`, one `find({_id: {$in: ...}})`, one `bulk_write` of updates and one `bulk_write` of deletes (`runner.py:675-780`).
3. Times each batch with `time.perf_counter()` (monotonic, correct clock) and then divides the batch time by the number of operations in the batch to get a per-operation latency (`runner.py:681`, `709`, `737`, `766`).

### 2.2 Percentiles

- Global p50/p95/p99 come from a 10,000-sample uniform reservoir (`metrics.py:265-273`, `297-304`).
- Per-second percentiles come from a 1,000-sample reservoir per second (`metrics.py:63-70`, `378-381`).
- Per-operation p95/p99 come from 5,000-sample reservoirs (`metrics.py:106-130`).
- In distributed mode, cluster-wide percentiles are the maximum of each worker's percentile (`distributed.py:205-207`).

### 2.3 Throughput control

There is no open model. Load is closed-loop: each worker sleeps a random think time per cycle and, if `target_ops_per_user_per_sec` is set, sleeps the rest of the cycle (`runner.py:786-791`).

### 2.4 Where the numbers are wrong

1. Per-operation latency is batch time divided by batch size. A bulk insert of 100 documents that takes 50 ms is recorded as 100 inserts of 0.5 ms each. Every operation in that batch actually waited 50 ms. Reported latencies are too low by a factor equal to the batch size. (`runner.py:681`, `696`, `709`, `722`, `737`, `750`, `766`, `779`).
2. Coordinated omission. When the server stalls, a closed-loop worker stops sending, so the stall shows up as one slow sample instead of many. Pacing with `target_ops_per_user_per_sec` measures from actual send time, not intended time (`runner.py:657`, `786-791`).
3. All operations in a cycle are stamped with the cycle start time (`runner.py:673`), so an operation that finishes in the next second is counted in the previous one.
4. Results are buffered and flushed every 500 operations (`runner.py:643`, `782-784`). At low rates nothing appears for many seconds. In the test run the progress log showed `total_ops=0` while the UI showed the test running (`docs/evidence/legacy-run-progress.log`).
5. Percentiles from reservoirs have sampling error that grows toward the tail and no stated precision. Per-second p99 from 1,000 samples is the 10th-largest sample.
6. Taking the maximum of per-worker percentiles is not a percentile of the combined distribution (`distributed.py:205-207`).
7. The findAndModify weight is never executed in the live loop. Its share is added to inserts by the "remainder" step (`runner.py:670-671`), so a 20% findAndModify mix silently becomes extra inserts.
8. The live loop ignores query targeting and per-collection schemas: it uses `DocumentGenerator(self._config.document_schema, rng)` and `_id` lookups only (`runner.py:625`, `660-664`). The UI still offers both settings.
9. The live loop never retries, so the retry policy shown as "applies now" has no effect on the load (`runner.py:675-780`, no `RetryController` in the loop).
10. Warm-up is not excluded from summaries; there is no warm-up concept.
11. There is no detection of a client-bound generator. The README claims 5-7K ops/sec per process (`README.md:236`), which is a Python GIL limit, but nothing warns when that limit is hit.

## 3. Bugs

Severity: critical means the tool reports wrong numbers or damages data; high means a feature does not work or the app fails under normal use; medium means wrong behavior in a less common path; low means cosmetic or minor.

| # | Severity | Location | Problem | How to reproduce | Proposed fix |
|---|---|---|---|---|---|
| B1 | critical | `engine/runner.py:681`, `709`, `737`, `766` | Per-operation latency is batch latency divided by batch size, so reported latency is too low. | Run with 50 users, 10 workers (5 ops per worker per cycle). Each recorded latency is one fifth of the real round trip. | Record the full batch round trip for every operation in the batch. In the new engine, send one operation per request. |
| B2 | critical | `engine/runner.py:657`, `786-791` | Coordinated omission: stalls are under-reported. | Pause the primary for 10 s (`db.fsyncLock()`), see p99 barely move. | Open model that measures from intended start time; report service time separately. |
| B3 | high | `engine/runner.py:665-671` | findAndModify share is executed as inserts. | Set mix to `find_and_modify: 1, insert: 0`. The table shows only inserts. | Dispatch findAndModify in the live loop. |
| B4 | high | `engine/runner.py:625`, `660-664` | Query targeting and per-collection schemas are ignored by the live loop. | Enable query targeting; server logs show only `_id` lookups. | Use the schema registry and query targets in the loop. |
| B5 | high | `engine/runner.py:675-780` | Retry policy shown as "Applies now" has no effect. | Set retry attempts to 1 vs 10 during a stepdown; error counts are identical. | Apply the retry controller per operation, or remove the claim. |
| B6 | high | `engine/runner.py:643`, `782-784` | Metrics flush every 500 operations, so live charts lag and the progress log reports 0 ops. | Run at low load (see section 2.4 item 4). | Flush at least once per second. |
| B7 | high | `config.py:329`, `engine/metrics.py:57-70`, `297-318` | Memory and CPU grow with run length: the default history is 86,400 s, each second keeps up to 1,000 floats, and every snapshot (once a second) re-sorts every bucket while holding the writers' lock. | 24 h run: up to 86.4 million samples kept; snapshot time grows linearly. | Bounded live window with downsampling; full resolution on disk. |
| B8 | high | `dashboard/app.py:3307-3353`, `dashboard/export.py:41-47` | PNG and PDF export fail on any host without Google Chrome. The failure is swallowed (`raise PreventUpdate`), so the button does nothing. | `python -c "import plotly.graph_objects as go; go.Figure().to_image(format='png')"` prints `Kaleido requires Google Chrome to be installed.` | Export charts from the browser canvas and produce an offline HTML report that prints to PDF. |
| B9 | high | `metadata/store.py:11-16`, `config.py:390` | The tool writes its own metadata into a `loader_metadata` database on the cluster under test. This needs write privileges outside the benchmark database and adds load to the cluster being measured. | `listDatabases` after one run shows `loader_metadata` (section 1.4). | Store state locally under `data/`. |
| B10 | medium | `engine/runner.py:1041-1045` | Scaling up sleeps 0.5 s per new worker inside the supervisor loop, blocking duration checks and stop handling. 200 workers take 100 s to start. | Change workers from 10 to 210 during a run; stop is ignored for up to 100 s. | Start workers without blocking the control loop. |
| B11 | medium | `engine/runner.py:545-546` | Stop joins each worker with a 5 s timeout in sequence; 200 stuck workers can take 1,000 s. | Stop during a network partition. | One shared deadline for all workers. |
| B12 | medium | `engine/runner.py:759-762` | Deleted ids are removed from the registry before the delete is sent; if the delete fails the id is lost. | Stop the primary while deleting. | Remove after a successful delete. |
| B13 | medium | `engine/runner.py:673` | All operations in a cycle are stamped with the cycle start, so they can land in the wrong second. | Any run with cycles longer than the remainder of a second. | Stamp at completion. |
| B14 | medium | `engine/distributed.py:205-207` | Distributed percentiles are a max of per-worker percentiles. | Two workers with different latency profiles. | Merge histograms. |
| B15 | medium | `engine/metrics.py:45` | Error classification matches the substring "auth", so any message containing "author" or "authSource" is classed as an authentication error; there is no use of error codes or labels. | Any error mentioning `authSource`. | Classify by code, code name, and error labels. |
| B16 | medium | `workload/registry.py:40` | With no per-collection override, `get_generator` builds a new `DocumentGenerator`, which builds a new `Faker()`, on every call. | Seeding 32 collections builds 32 Faker instances; the executor path does it per operation. | Cache the default generator per namespace. |
| B17 | low | `workload/registry.py:29` | Seeds use `hash(key)`, which Python randomizes per process, so runs are not reproducible. | Two runs with the same `random_seed` generate different documents. | Use a stable hash. |
| B18 | low | `engine/control.py:133` | A runtime URI equal to `mongodb://localhost:27017` is never remembered, so metadata sync can replace it. | Use the default local URI and sync config. | Compare to "not set", not to a literal. |
| B19 | low | `engine/runner.py:1063-1065` | The error-rate check thread reads `self._running` without the lock. | Race only. | Use the locked property. |

## 4. Security findings (against brief section 11)

| # | Finding | Evidence | Fix |
|---|---|---|---|
| S1 | The sample config binds the dashboard to all interfaces over plain HTTP with no access control. Anyone who can reach port 8050 can start load against the cluster. | `config/loadtest.sample.yaml:65` (`host: "0.0.0.0"`), `cli.py:179-188` | Default to `127.0.0.1`, HTTPS, optional access token. |
| S2 | No TLS support at all for the UI. | `cli.py:179-188` (Waitress has no TLS) | TLS modes `off`, `files`, `self-signed`. |
| S3 | No cross-site request protection. Dash callbacks are POSTs to `/_dash-update-component` with no origin check or custom header requirement, so a malicious page in the operator's browser can press Start. | `app.py:2288` (callbacks), no middleware in `cli.py` | Same-origin check plus a required custom header. |
| S4 | No security headers (CSP, `X-Content-Type-Options`, `Referrer-Policy`, `frame-ancestors`). | `grep -rn "csp\|Talisman" mongo_load_tester` returns nothing. | Add headers on every response. |
| S5 | The connection string, including the password, is stored in plain YAML with no permission check. | `config/loadtest.sample.yaml:7`, `config.py:428-445` | Saved targets in `data/` with mode 0600; secrets from env or file with a permission warning. |
| S6 | Exception text is logged and shown in the UI without redaction (`_feedback_error(f"Control action failed: {exc}")`). PyMongo errors usually contain host names; a malformed URI can echo parts of it. | `app.py:2540`, `cli.py:61` | Redaction filter on all log output and API errors. |
| S7 | Request bodies are not size limited and inputs are parsed ad hoc (`int(duration_value)`). | `app.py:2438` | Schema validation and a body size limit. |
| S8 | No dependency vulnerability scanning and no SBOM. | No CI exists. | `pip-audit` in CI; CycloneDX SBOM per release. |
| S9 | No shell or server-side evaluation of user input was found. | `grep -rn "subprocess\|os.system\|eval(\|exec(" mongo_load_tester` returns nothing. | Keep it that way. |

## 5. Deployment gaps (against brief sections 9 and 10)

### 5.1 Assumptions that break a no-root, offline install

1. A system Python 3.11+ with `pip` and `venv`. Ubuntu cloud images ship `python3` but not `python3-venv`, which needs `apt install` (root).
2. Internet access at install time to PyPI (`pip install`). Nothing is vendored.
3. Google Chrome for PNG/PDF export (B8). This is a system package.
4. The current working directory: logs and exports are written relative to it (`config.py:336`, `config.py:349`).
5. Network access to the cluster for state, even when only browsing old results (B9).

### 5.2 Missing against the brief

- No HTTPS, certificate generation, SIGHUP reload, HSTS, redirect listener, or proxy support (section 9).
- No bundle, no `make release`, no checksums, no notices, no SBOM, no CI (section 10.2).
- No control script, supervisor, PID files, autostart, or health command (section 10.3).
- No startup check of writable directories or free disk (section 10.3 item 7).
- No check that the listen port is above 1024 (section 10.1 item 4); the default is 8050, which is fine, but any value 1-65535 is accepted (`config.py:327`).
- No environment variable overrides or flag precedence (section 8).

### 5.3 Can the current stack produce the offline bundle?

Yes, with smaller fixes than a rewrite:

- A relocatable CPython build (python-build-standalone, BSD-3) can be shipped inside the bundle. Its release download was reachable from this environment (`curl -r 0-1000 .../cpython-3.12.7+20241016-aarch64-unknown-linux-gnu-install_only.tar.gz` returned HTTP 206).
- Wheels for linux x86_64, linux aarch64 and macOS can be fetched with `pip download --platform ... --only-binary=:all:` and installed into the bundle's `lib/`.
- Chrome is only needed by Kaleido; removing server-side image rendering removes the system dependency.

So the backend stays in Python (see `docs/DECISIONS.md`, D-002). The Dash UI is replaced because the brief requires server-sent events or WebSockets (7.3.7), a strict CSP (11.7), and designed states that Dash's polling single-callback model does not provide; that is a requirement-driven change, not a language change.

## 6. UI and UX gaps

Evidence: `docs/design/before/*.png` and `dashboard/app.py`.

1. One 4,160-line page. Configuration, live controls, charts, logs and a schema editor share one scroll. There is no navigation, no run history, no report view, no comparison (`app.py` has no routes).
2. 354 inline `style=` dictionaries (`grep -c "style=" app.py`). Colors and spacing are hard-coded per element, so there is no theme, no dark mode, and no density setting.
3. Zero `aria-*` or `role` attributes (`grep -c "aria-\|role=" app.py` prints 0). Help is hover-only `(?)` icons, which keyboard and touch users cannot open.
4. Buttons are always visible regardless of state; Start is styled as white text on the header gradient and is hard to see (`03-running-top.png`).
5. No loading, empty, or error states: before a run every chart is an empty axis; on refresh failure the whole page shows an error payload (`app.py:4005`).
6. Export buttons fail silently (B8).
7. Wording is inconsistent: "Apply duration", "Apply load controls", "Apply resilience settings", status badges in all caps ("APPLIES NOW", "STAGED").
8. "Reset all settings to defaults" sits next to the duration field as a red primary button.
9. Branding uses a third-party insurer's color palette (commit `e88f291`, "Update color scheme to Allstate brand colors"), which must not ship in a product sold to others.
10. Times are shown only in UTC; there is no local time display.
11. No browser keyboard shortcuts; no way to add a marker.

## 7. Licensing risk for commercial sale

No GPL, AGPL, or SSPL package is in the dependency tree (section 1.2). Points to note:

- `certifi` is MPL-2.0: file-level copyleft. Shipping it unmodified with its license text is allowed in a closed product.
- `waitress` is ZPL-2.1, permissive.
- `simplejson` is dual MIT or AFL-2.1; choose MIT.
- The product itself had no license file (`README.md:313` says "See repository for license details").
- MongoDB server binaries (SSPL) must not ship in the bundle. They are only used in development and CI through Docker images.

## 8. Plan

Ordered by the priority in brief section 2. Estimates are for one engineer.

| # | Work item | Estimate | Risk of not doing it |
|---|---|---|---|
| P1 | Fix B1, B3, B6, B12, B15, B16, B17 in the legacy engine so the existing feature set reports honest numbers while it is still available. | 1 h | Wrong numbers from anyone still using the legacy mode. |
| P2 | New measurement core: HDR-equivalent histograms, response and service time, error classification by code and label, impact analysis, client-bound detection, with tests on synthetic data. | 4 h | The scale-event use case cannot be trusted. |
| P3 | New engine: open and closed models, schedules, data load, generator process, driver event capture, server sampling, markers, local SQLite storage, interrupted runs, run snapshots. | 6 h | No markers, no event timeline, no history. |
| P4 | Configuration (file, env, flags), paths relative to install dir, HTTPS modes, SIGHUP reload, security middleware, access gate, supervisor, `ballastctl`, bundle build, CI. | 5 h | Cannot be installed on a host without root. |
| P5 | New UI per `docs/design/DESIGN.md`: runs, new run, live run, report, compare, targets, profiles, settings, system. | 6 h | Product does not look sellable. |
| P6 | Reports (offline HTML, CSV, JSON), comparison, documentation, integration and failover tests, browser tests, soak, fresh-host test. | 5 h | Unverified claims. |

## 9. Open questions and the guesses used

Each is copied into `docs/DECISIONS.md`.

1. Branch name: the brief says `pagombin/enterprise-redesign`, the session is pinned to `claude/lucid-goodall-0s1ayr`. Guess: use the session branch (D-001).
2. Keep Python or rewrite in Go? Guess: keep Python, replace Dash (D-002).
3. What happens to the legacy Dash app and its distributed leader/follower mode? Guess: keep it, fixed where cheap, behind `features.legacy_dashboard` (off by default) and the `ballast legacy` command (D-003).
4. The legacy app spreads load over many databases; the brief says the tool writes only to one named database. Guess: new engine uses one named database with N collections (D-004).
5. How are collections tagged? Guess: a registry collection `_ballast_collections` inside the benchmark database (D-005).
6. How are marker pairs formed? Guess: explicit `begin`/`end` phase, inferred from labels ending in "start(ed)" or "finish(ed)/end/done" when not given (D-006).
7. PNG and PDF export depend on Chrome. Guess: PNG from the browser canvas, PDF by printing the offline HTML report (D-007).
