# MongoDB Load Tester (Modular)

A configurable Python load/stress testing tool for MongoDB clusters, designed for OLTP-like workloads with:

- virtual users + worker thread control
- weighted read/insert/update/delete mix
- multiple databases and collections
- schema-driven payload generation
- live dashboard with timelines and latency metrics
- in-browser export (CSV, PNG, PDF)

## Architecture

```text
mongo_load_tester/
  config.py               # Typed config models + YAML loader
  models.py               # Shared metric/result data classes
  cli.py                  # Commands: run, validate, init-config
  engine/
    client.py             # MongoClient factory
    executor.py           # Single-op execution logic (read/write/update/delete)
    id_registry.py        # Thread-safe known _id cache for realistic reads/updates
    metrics.py            # Lock-safe rolling aggregation + snapshots
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
  uri: "mongodb://<username>:<password>@<host1>:27017,<host2>:27017/?replicaSet=<rs_name>"
```

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
- Runtime progress bar with elapsed and remaining time
- Adjustable timeline window (1m, 5m, 15m, all)
- KPI cards for throughput, error rate, and latency percentiles
- Operation and latency timelines with unified hover tracking
- Operation distribution chart and per-operation summary table
- One-click exports from browser:
  - timeline CSV
  - PNG chart image
  - PDF report
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

## Notes for Production-Scale Runs

- Ensure your MongoDB connection string enables expected topology/replica set settings.
- Start with lower `worker_threads` and increase gradually.
- Use dedicated load-generator hosts for high-throughput tests.
- Watch server-side metrics (CPU, IOPS, WT cache, lock contention, replication lag) in parallel.
- Increase `mongo.max_pool_size` for larger concurrency.
