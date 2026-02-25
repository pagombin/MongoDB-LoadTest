# MongoDB Load Tester

A production-grade, config-driven MongoDB load and stress testing application with a live dashboard, designed for OLTP-like workloads at scale.

## Key Features

- **5 operation types**: read, insert, update, delete, and findAndModify with customizable query targeting
- **14 field types**: string (with prefix/suffix/UUID composites), int, float, decimal, bool, choice, datetime (4 modes), objectid, uuid, faker (25+ providers), object (embedded documents), array, null
- **Per-collection schemas**: different document structures for different collections, all configurable from the UI
- **Schema Builder**: visual field-by-field schema editor with type selection, rules, and per-database/collection targeting
- **Operation Targeting**: configure per-operation filter fields, projections, sort order, and update operators ($set, $inc, $unset, $push, $currentDate)
- **Live dashboard**: real-time charts, KPIs, controls, and exports — all in browser
- **Distributed mode**: run multiple worker processes for 25-40K+ ops/sec with a unified dashboard view
- **Metadata persistence**: all configuration, runs, events, and per-collection schemas stored in MongoDB

## Architecture

```text
mongo_load_tester/
  config.py               # Typed config models + YAML loader
  models.py               # Shared metric/result data classes
  cli.py                  # Commands: run, validate, init-config
  observability.py        # Logging setup + in-memory UI log buffer
  engine/
    client.py             # MongoClient factory
    executor.py           # Operation execution (read/insert/update/delete/findAndModify)
    id_registry.py        # Thread-safe O(1) document _id cache (swap-and-pop, per-namespace locks)
    metrics.py            # Lock-safe rolling aggregation with reservoir sampling + batch recording
    retry.py              # Retry controller (exponential backoff + symmetric jitter)
    runner.py             # Concurrency orchestration + ramp-up + seeding + worker scaling
    control.py            # Thread-aware control facade for dashboard
    distributed.py        # Multi-process metrics publishing + aggregation via MongoDB
  workload/
    schema.py             # Document generation with faker, nested structures, composites
    selection.py          # Namespace and weighted operation selectors (with findAndModify)
    registry.py           # Per-collection SchemaRegistry with default fallback
  metadata/
    store.py              # MongoDB-backed config, run, event, and per-collection schema persistence
    sync.py               # Background config sync service
  dashboard/
    app.py                # Dash UI + callbacks + Schema Builder modal
    figures.py            # Chart builders (11 charts)
    export.py             # CSV/PNG/PDF export helpers
```

## Installation

### Prerequisites

- Python 3.10+
- Network access to a MongoDB cluster
- A valid MongoDB connection string

### Install with pip (no virtual environment)

```bash
pip install dash faker kaleido plotly pydantic pymongo pyyaml reportlab typer
cd MongoDB-LoadTest
pip install .
```

### Install with virtual environment (recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quick Start

### 1. Generate a config file

```bash
mongo-load-tester init-config --output config/loadtest.yaml
```

Or copy and edit `config/loadtest.sample.yaml`.

### 2. Set your MongoDB URI

```yaml
mongo:
  uri: "mongodb+srv://<user>:<pass>@<cluster>.mongodb.net/?retryWrites=true&w=majority"
  tls: null
```

### 3. Validate

```bash
mongo-load-tester validate --config config/loadtest.yaml
```

### 4. Run with dashboard

```bash
mongo-load-tester run --config config/loadtest.yaml
```

Open `http://127.0.0.1:8050` in your browser. The test does **not** auto-start — configure settings in the UI, then click **Start**.

### 5. Run headless

```bash
mongo-load-tester run --config config/loadtest.yaml --no-dashboard
```

### 6. Run distributed (25-40K+ ops/sec)

```bash
# Terminal 1-3: headless workers (each gets its own Python GIL)
mongo-load-tester run -c config/loadtest.yaml --no-dashboard \
  --worker-id w1 --load-test-id perf-test
mongo-load-tester run -c config/loadtest.yaml --no-dashboard \
  --worker-id w2 --load-test-id perf-test
mongo-load-tester run -c config/loadtest.yaml --no-dashboard \
  --worker-id w3 --load-test-id perf-test

# Terminal 4: dashboard (unified view of all workers)
mongo-load-tester run -c config/loadtest.yaml --load-test-id perf-test
```

The dashboard shows a **Distributed Workers** panel with per-worker ops/sec, total ops, p95 latency, and aggregate throughput.

## Dashboard

### KPIs (10)

Total ops, ops/sec, error rate, success rate, p50/avg/p95/p99 latency, retry rate, active users.

### Charts (11)

| Chart | Description |
|-------|-------------|
| Operations Throughput | Total, read, and write ops/sec over time |
| Users Ramp View | Active vs target virtual users with pause overlays |
| Latency Percentiles | p50, avg, p95, p99 bands over time |
| Errors & Retries | Error/retry counts + error rate % (dual axis) |
| Throughput by Operation Type | Stacked area: read/insert/update/delete/findAndModify |
| Latency by Operation Type | Per-operation average latency over time |
| Operation Mix | Donut chart of cumulative operation distribution |
| Error Breakdown | Categorized errors: timeout, network, primary_stepdown, etc. |
| Latency Distribution | Histogram with cumulative % overlay |
| Operation Breakdown | Table: count, share, avg/p95/p99 latency per operation |
| Smart Insights | Automated health analysis with thresholds |

Every chart title has a **(?)** tooltip with a detailed explanation.

### Controls

- **Start / Pause / Resume / Stop / Restart** — full lifecycle control
- **Live controls** (apply immediately): virtual users, worker threads, think time, operation mix, retry policy, log level
- **Run Configuration** (apply before start or staged for next run): namespace, databases, collections, seeding, ramp-up, schema
- **Schema Builder** — visual modal for field-by-field schema creation with per-collection targeting
- **Operation Targeting** — configure per-operation filter fields, update operators, projections, sort
- **Reset to defaults** — resets Run Configuration, Schema Builder, and per-collection schema overrides
- **Exports**: CSV, PNG, PDF

### Worker Scaling

- **Scale down**: excess workers are dismissed immediately (within one operation cycle)
- **Scale up**: new workers are staggered 0.5s apart to avoid thundering-herd connections
- **Hard limit**: `max_worker_threads` (default 500) caps worker count; requests above the limit are clamped with a visible warning
- **Error monitoring**: if error rate exceeds 10% after scaling up, a red alert banner appears for 8 seconds

## Schema Configuration

### Field Types

| Type | Properties | Example |
|------|-----------|---------|
| `string` | `length`, `prefix`, `suffix`, `composite` | `{"name":"id","field_type":"string","length":8,"prefix":"ORD-"}` |
| `int` | `min_value`, `max_value` | `{"name":"qty","field_type":"int","min_value":1,"max_value":100}` |
| `float` | `min_value`, `max_value` | `{"name":"price","field_type":"float","min_value":0.01,"max_value":999}` |
| `decimal` | `min_value`, `max_value` | `{"name":"amount","field_type":"decimal"}` |
| `bool` | — | `{"name":"active","field_type":"bool"}` |
| `choice` | `choices[]` | `{"name":"status","field_type":"choice","choices":["new","paid"]}` |
| `datetime` | `date_mode`, `date_range_days` | `{"name":"ts","field_type":"datetime","date_mode":"random_past"}` |
| `objectid` | — | `{"name":"ref","field_type":"objectid"}` |
| `uuid` | — | `{"name":"trace","field_type":"uuid"}` |
| `faker` | `faker_provider` | `{"name":"email","field_type":"faker","faker_provider":"email"}` |
| `object` | `fields[]` | Embedded documents with sub-fields |
| `array` | `items`, `min_items`, `max_items` | Arrays of primitives or embedded documents |
| `null` | — | Always null (sparse fields) |

### Nested Structures

```json
{"name": "address", "field_type": "object", "fields": [
  {"name": "street", "field_type": "faker", "faker_provider": "street_address"},
  {"name": "city", "field_type": "faker", "faker_provider": "city"}
]}
```

```json
{"name": "tags", "field_type": "array",
 "items": {"field_type": "string", "length": 10},
 "min_items": 1, "max_items": 5}
```

```json
{"name": "line_items", "field_type": "array",
 "items": {"field_type": "object", "fields": [
   {"name": "sku", "field_type": "string", "length": 12},
   {"name": "qty", "field_type": "int", "min_value": 1, "max_value": 10}
 ]}, "min_items": 1, "max_items": 8}
```

### Date Modes

- `now` — current UTC timestamp
- `random_past` — random within `date_range_days` ago
- `random_future` — random within `date_range_days` ahead
- `increment` — starts from now, adds `date_increment_seconds` per document

### String Composites

- `prefix_uuid` — `"ORD-" + UUID`
- `uuid_suffix` — `UUID + "-US"`
- `prefix_uuid_suffix` — `"ORD-" + UUID + "-US"`
- `prefix_random` — `"ORD-" + random(length)`
- `random_suffix` — `random(length) + "-US"`

### Faker Providers

`name`, `first_name`, `last_name`, `email`, `phone_number`, `company`, `address`, `city`, `country`, `zipcode`, `url`, `ipv4`, `user_agent`, `job`, `text`, `sentence`, `paragraph`, `word`, `credit_card_number`, `iban`, `ssn`, `license_plate`, `color_name`, `currency_code`, `file_name`

## Operation Targeting

Configure how each operation type finds and modifies documents:

```json
{
  "enabled": true,
  "read": {
    "filter_fields": ["tenant_id", "status"],
    "projection_fields": ["order_total", "status"],
    "sort_field": "updated_at",
    "sort_direction": "desc"
  },
  "update": {
    "filter_fields": ["tenant_id"],
    "operators": {
      "set_fields": ["status"],
      "inc_fields": ["priority"],
      "current_date_fields": ["updated_at"]
    }
  },
  "delete": {"filter_fields": ["status"]},
  "find_and_modify": {
    "filter_fields": ["tenant_id"],
    "operators": {"set_fields": ["status"], "current_date_fields": ["updated_at"]},
    "sort_field": "priority",
    "sort_direction": "asc",
    "return_updated": true,
    "upsert": false
  },
  "create_indexes": true
}
```

### Update Operators

- `set_fields` — `$set` with generated values from your schema
- `inc_fields` — `$inc` by random 1-10
- `unset_fields` — `$unset` to remove fields
- `push_fields` — `$push` generated values onto arrays
- `current_date_fields` — `$currentDate` for timestamps

## Metadata

All state is persisted in MongoDB under the `loader_metadata` database (configurable):

| Collection | Purpose |
|-----------|---------|
| `load_test_configs` | Configuration per `load_test_id` with versioning |
| `load_test_runs` | Run lifecycle records with full metrics summaries |
| `load_test_events` | Audit trail for config changes and control actions |
| `collection_schemas` | Per-collection schema overrides |
| `load_test_live_metrics` | Distributed worker metrics (for multi-process mode) |

All collections are indexed. Queries use projections and hints for optimal performance.

## Performance Tuning

| Setup | Expected ops/sec |
|-------|-----------------|
| Single process, default config | 5-7K |
| Single process, tuned (no faker, no think time) | 12-15K |
| 2 headless + 1 dashboard | 25-35K |
| 3 headless + 1 dashboard | 35-50K |
| 4 headless (no dashboard) | 45-60K |

### Tips

- Set `think_time_ms_min` and `think_time_ms_max` to 0 for maximum throughput
- Use simple schemas (avoid faker) for throughput tests
- Disable operation targeting for `_id`-only lookups (fastest)
- Increase `mongo.max_pool_size` to match concurrency
- Per-thread metric batching reduces lock contention by 50x (built-in)
- For 25K+ ops/sec, use distributed mode with multiple `--worker-id` processes

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

66 tests covering: config validation, metrics aggregation, executor operations, id registry, schema generation, retry logic, runner controls, dashboard helpers, and more.

## License

See repository for license details.
