# MongoDB Load Tester (Modular)

A configurable Python load/stress testing tool for MongoDB clusters, designed for OLTP-like workloads with:

- virtual users + worker thread control
- weighted read/insert/update/delete mix
- multiple databases and collections
- schema-driven payload generation
- live dashboard with timelines and latency metrics
- metadata-backed configuration persistence in MongoDB (`loader_metadata`)
- pause/resume controls and live duration updates from dashboard
- live load shaping controls (target users, pacing, operation mix, retry, logging)
- users ramp-up/ramp-down timeline
- live log stream in dashboard (with level/tail filters)
- fail-safe exception capture with graceful error reporting
- in-browser export (CSV, PNG, PDF)

## Architecture

```text
mongo_load_tester/
  config.py               # Typed config models + YAML loader
  models.py               # Shared metric/result data classes
  cli.py                  # Commands: run, validate, init-config
  observability.py        # Logging setup + in-memory UI log buffer
  engine/
    client.py             # MongoClient factory
    executor.py           # Single-op execution logic (read/write/update/delete)
    id_registry.py        # Thread-safe known _id cache for realistic reads/updates
    metrics.py            # Lock-safe rolling aggregation + snapshots
    retry.py              # Retry controller (exponential backoff + jitter)
    runner.py             # Concurrency orchestration + ramp-up + seeding
  workload/
    schema.py             # Synthetic document generation from schema config
    selection.py          # Namespace and weighted operation selectors
  dashboard/
    app.py                # Dash UI + callbacks
    figures.py            # Plot builders
    export.py             # CSV/PNG/PDF export helpers
```

## Installation and Running the Script

### 1) Prerequisites

- Python 3.10+
- Network access to your MongoDB cluster
- A valid MongoDB connection string

### 2) Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
```

### 3) Install the project

```bash
python3 -m pip install -e .
```

Optional dev dependencies (tests, etc.):

```bash
python3 -m pip install -e ".[dev]"
```

### 4) Create a config file

Generate a starter config:

```bash
python3 -m mongo_load_tester.cli init-config --output config/loadtest.yaml
```

Or copy and edit `config/loadtest.sample.yaml`.

### 5) Configure your MongoDB URI

Open `config/loadtest.yaml` and set:

```yaml
mongo:
  uri: "mongodb://<user>:<pass>@<host1>:27017/?authSource=admin"
  # Keep null to use URI defaults (recommended, especially for mongodb+srv)
  tls: null
```

Supported connection string patterns:

- Replica set:

```yaml
mongo:
  uri: "mongodb://<user>:<pass>@<host1>:27017,<host2>:27017,<host3>:27017/?replicaSet=<rs_name>&authSource=admin"
  tls: null
```

- Sharded cluster (mongos routers):

```yaml
mongo:
  uri: "mongodb://<user>:<pass>@<mongos1>:27017,<mongos2>:27017/?authSource=admin"
  tls: null
```

- SRV (Atlas/managed):

```yaml
mongo:
  uri: "mongodb+srv://<user>:<pass>@<cluster>.mongodb.net/?retryWrites=true&w=majority"
  tls: null
```

The runner uses PyMongo topology discovery, so it works with replica set and sharded deployments as long as the URI is valid and reachable.

### 6) Validate config

```bash
python3 -m mongo_load_tester.cli validate --config config/loadtest.yaml
```

### 7) Run with dashboard (recommended)

```bash
python3 -m mongo_load_tester.cli run --config config/loadtest.yaml
```

Open the dashboard in your browser at:

```text
http://<dashboard-host>:<dashboard-port>
```

Defaults are `127.0.0.1:8050` unless changed in config.

Important behavior:

- The load test does **not** auto-start on dashboard launch.
- Default settings are loaded from metadata in MongoDB (`loader_metadata`) for the configured `metadata.load_test_id`.
- Review/update settings in the UI, then click **Start** to begin the run.

### 8) Run headless (no UI)

```bash
python3 -m mongo_load_tester.cli run --config config/loadtest.yaml --no-dashboard
```

### CLI shortcut

After installation, you can also use the console script directly:

```bash
mongo-load-tester run --config config/loadtest.yaml
```

If that command is not in your PATH, use the `python3 -m mongo_load_tester.cli ...` form above.

## Dashboard UX Highlights

- Live status badge (running/completed/failed/initializing)
- Start control in UI (run is explicitly started by user)
- Pause/resume/stop controls directly in UI
- One-click restart from UI with existing settings (metrics reset for clean rerun)
- Runtime duration can be updated while the test is running
- Phase 1 live controls can be applied immediately without restart:
  - target virtual users (can scale up/down live)
  - worker thread target (active threads may be capped by current running pool until restart)
  - think-time min/max and target ops/user/sec pacing
  - read/insert/update/delete mix weights
  - retry policy (attempts + backoff + jitter)
  - runtime log level and worker progress-log cadence
- Runtime progress bar with elapsed and remaining time
- Adjustable timeline window (1m, 5m, 15m, 30m, 1h, 8h, 24h, all)
- Smarter KPI cards for throughput, success/error, latency, retries, and active users
- Ops/sec card includes tooltip explaining active-runtime computation (pause time excluded)
- Operation and latency timelines with unified hover tracking
- Users ramp timeline (active virtual users over time)
- Users ramp timeline includes target-users trace
- Operation distribution chart and per-operation summary table
- Smart insights panel highlighting health/trend signals
- Live control-state strip with recent runtime change history
- Metadata sync notice in UI (external config changes can take up to 30s to apply)
- Live logs panel in UI (INFO/WARNING/ERROR filters and adjustable tail lines)
- One-click exports from browser:
  - timeline CSV (includes active users + retries)
  - PNG combined timeline image (operations + users)
  - PDF report (operations, latency, users)
- In-dashboard "Stop test" control for graceful shutdown

## Config Model

See `config/loadtest.sample.yaml` for a full template.

Key knobs:

- `load.virtual_users`: logical users to simulate
- `load.worker_threads`: actual worker thread pool
- `load.target_ops_per_user_per_sec`: optional pacing cap
- `operation_mix`: traffic mix percentages
- `namespace.databases` + `namespace.collections_per_database`: namespace spread
- `schema.fields`: generated document fields
- `logging.*`: log level, file rotation, UI buffer, and progress log cadence
- `retry.*`: max attempts + exponential backoff + jitter for transient Mongo errors
- `metadata.database_name`: metadata DB name (default `loader_metadata`)
- `metadata.load_test_id`: unique configuration scope per load test
- `metadata.sync_interval_seconds`: background metadata polling interval (default 30s)

### Exception safety

- Global exception hooks capture uncaught exceptions in main/threaded/async contexts.
- Runner and workers capture unexpected failures, log full stack traces, and shut down gracefully.
- CLI commands return controlled non-zero exits instead of crashing with unhandled traces.
- Dashboard callbacks are guarded to avoid hard UI breakage from callback/runtime errors.

### Retry strategy

The runner uses a centralized retry controller for transient MongoDB failures:

- exponential backoff with jitter
- configurable max attempts and bounds
- retry classification for common transient PyMongo exceptions and retry labels
- applies to:
  - connection ping
  - initial seeding batches
  - runtime read/insert/update/delete operations

### Metadata-backed configuration model

Configuration state is stored in the target cluster under the `loader_metadata` database:

- `load_test_configs`: single upserted config document per `load_test_id`
- `load_test_runs`: run lifecycle/status records
- `load_test_events`: configuration and control-plane audit events

The dashboard writes UI configuration changes back to metadata immediately. A background sync service also polls metadata every 30 seconds (configurable) and applies externally updated settings.

Optional CLI override for config scope:

```bash
python3 -m mongo_load_tester.cli run --config config/loadtest.yaml --load-test-id perf-team-a
```

## Notes for Production-Scale Runs

- Ensure your MongoDB connection string enables expected topology/replica set settings.
- Start with lower `worker_threads` and increase gradually.
- Use dedicated load-generator hosts for high-throughput tests.
- Watch server-side metrics (CPU, IOPS, WT cache, lock contention, replication lag) in parallel.
- Increase `mongo.max_pool_size` for larger concurrency.
