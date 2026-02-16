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

## Install

```bash
pip install -e .
```

Optional dev dependencies:

```bash
pip install -e ".[dev]"
```

## Quick Start

1. Generate starter config:

```bash
mongo-load-tester init-config --output config/loadtest.yaml
```

2. Validate config:

```bash
mongo-load-tester validate --config config/loadtest.yaml
```

3. Run with dashboard:

```bash
mongo-load-tester run --config config/loadtest.yaml
```

4. Run headless:

```bash
mongo-load-tester run --config config/loadtest.yaml --no-dashboard
```

The dashboard has buttons to export:

- timeline metrics as CSV
- operations chart as PNG
- summary report as PDF

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
