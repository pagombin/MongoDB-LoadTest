# Changelog

## 1.0.0 (2026-10-23)

First release under the name Ballast. The previous tool (`mongo_load_tester`) is kept as the legacy dashboard behind `features.legacy_dashboard`.

### Added

- Open-model load (target rate, latency from intended start) and closed-model load (workers, think time), with warm-up, ramp steps, steady duration or run until stopped, and a data load phase.
- Workload profiles: operation mix (find by key, range find, insert, update, upsert, delete, aggregate, find-and-modify), document size and shape (including the legacy schema field types), key distributions (uniform, zipfian, sequential), indexes, and all main driver settings. Save, duplicate, export and import.
- Markers from the UI (M), the API (`POST /api/runs/{id}/markers`) and the command line (`ballastctl mark`).
- Driver topology and connection pool events on the run timeline; optional `hello`, `serverStatus` and `replSetGetStatus` sampling that degrades when privileges are missing.
- Error classification by code, code name and labels, counted per second with exact first and last failure times.
- Impact analysis per marker pair, selected range or detected disruption: failures by class, longest write and read gaps, primary changes, p50/p99/max before, during and after, and recovery time.
- Client-bound detection (rate shortfall, generator CPU, pool checkout wait).
- HDR-layout histograms (0.1% precision) per second and per operation; percentiles over any period come from merged histograms.
- Run report with offline HTML export (prints to PDF), CSV and JSON exports, and run comparison aligned at start or at a marker.
- New UI: runs, new run, live run, report, compare, targets, profiles, settings, system; light and dark themes; comfortable and compact density; local or UTC time; keyboard shortcuts.
- HTTPS by default with a self-signed certificate generated without openssl, `files` mode, SIGHUP reload, optional HTTP redirect listener, reverse proxy support.
- Optional access token gate; CSRF protection; strict Content-Security-Policy and security headers; schema validation and body size limits; passwords redacted everywhere.
- Configuration file with `BALLAST_*` environment overrides and flags; `ballast config validate` and `ballast config print`.
- `ballastctl` with a built-in supervisor (crash restart with backoff), PID files, status and health exit codes, log rotation, and autostart via systemd user unit or crontab.
- Self-contained release bundles for Linux x86-64, Linux ARM64 and macOS Apple Silicon, with checksums, third-party notices and a CycloneDX SBOM.
- Local storage in SQLite under `data/` with backed-up schema migrations; runs active during a restart are marked interrupted and keep their data.
- Installs with the host's Python 3.10 or later and pip, needing only `aiohttp`, `pydantic` and `pymongo`: `scripts/install.sh` works without `python3-venv`, and `scripts/make-wheelhouse.sh` builds an offline kit. PyYAML and Faker are optional; `cryptography` and `openssl` are not used.
- `ballast doctor` checks a host and prints the command for anything missing; the System page lists the same.
- Guardrails that stop a run when failures or p99 stay over a limit, and pass/fail criteria (SLO) per profile.
- `ballast run` for headless runs in CI, with exit codes for pass, fail, guardrail and errors.
- Webhook notifications, Prometheus `/metrics`, `ballastctl backup` and `restore`.
- Run notes and tags, Run again, Copy summary (Markdown), Markdown export, tag filter and filters kept in the address, live progress bar and tab title, desktop notifications, compare deltas, a Ctrl+K command palette.
- macOS Intel bundle (`darwin-amd64`).

### Fixed (legacy engine)

- Per-operation latency in bulk requests was the batch time divided by the batch size, so reported latency was too low. Each operation now records the full round trip.
- findAndModify weight was executed as inserts.
- Metrics were flushed only every 500 operations, so live charts lagged and progress showed zero.
- Deleted ids left the id registry before the delete succeeded.
- Operations were stamped with their cycle start instead of completion.
- Any error message containing "auth" was classified as an authentication error.
- A new Faker instance was built per call for collections without a schema override; seeds used Python's randomized `hash()`.

### Changed

- The tool no longer writes its own metadata into the cluster under test; state is local.
- The Allstate color palette in the legacy UI is replaced by the Ballast design in the new UI.
