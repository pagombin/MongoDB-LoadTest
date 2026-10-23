# Verification

Everything below was run in this environment on 2026-10-23 against the commit it is pushed with. Each section names the command and the file under `docs/evidence/` holding its full output.

Environment: Linux x86-64, 4 CPUs, 15.7 GB RAM; Docker 29.9.1; MongoDB 8.0.34 and 7.0.45 in three-member replica sets started with `scripts/dev-replset.sh`; Chromium 1194 through Playwright 1.56.1.

## Summary

| Check (brief section) | Result | Evidence |
|---|---|---|
| Unit tests (12.1) | 141 passed, also with `-W error` | `docs/evidence/unit-tests.txt` |
| Integration on MongoDB 8.0, including failover (12.2) | 5 passed | `docs/evidence/integration-mongo-8.0.txt` |
| Integration on MongoDB 7.0 (12.2, 16.4) | 5 passed | `docs/evidence/integration-mongo-7.0.txt` |
| Leak checks (12.3) | passed on both versions | `tests/integration/test_leaks.py` in the two files above |
| Soak, 2 hours (12.4) | see section 5 | `docs/evidence/soak.csv`, `docs/evidence/soak.svg` |
| Browser end-to-end (12.5) | 17 of 17 checks passed | `docs/evidence/e2e-browser.txt` |
| Accessibility scan, WCAG 2.2 A/AA rules (7.3.3) | 0 violations, 16 page and theme combinations | `docs/evidence/a11y-axe.txt` |
| Lint, format, type checks (12.6) | 0 errors, 0 warnings | `docs/evidence/static-checks.txt` |
| Dependency vulnerabilities (11.9) | No known vulnerabilities in 53 pinned packages | `docs/evidence/pip-audit.txt` |
| Simulated fresh host (12.7) | 23 of 23 host checks passed, plus the steps in section 7 | `docs/evidence/fresh-host.log` |
| Install from git (10.1.2) | passed | `docs/evidence/git-install.txt` |
| Certificate reload on SIGHUP (9.3) | passed | `docs/evidence/sighup-reload.txt` |
| Legacy dashboard behind its flag (2.3) | refused when off, runs when on | `docs/evidence/legacy-flag.txt` |
| Release bundles (10.2) | linux-amd64, linux-arm64, darwin-arm64 with SHA-256, notices and SBOM | section 8 |

## 1. Unit tests

```
runtime/python/bin/python3 -m pytest tests -v -W error
```

141 tests in `tests/unit` (new code) and `tests/legacy` (the original suite, all still passing). What they cover, by brief item:

| Brief 12.1 item | Tests |
|---|---|
| Configuration parsing and precedence | `tests/unit/test_config.py`: flag over env over file over default, typed env values, unknown keys and values name the key, unknown `BALLAST_*` variables, reserved variables, `tls.mode: off` surviving YAML boolean parsing, secrets from inline/env/file with the permission warning, redaction |
| Path resolution | `test_relative_paths_resolve_against_install_dir_not_cwd`, `test_default_config_file_in_install_dir` |
| Histogram merging and percentiles | `tests/unit/test_histogram.py`: 200,000 samples from uniform, exponential, lognormal and bimodal distributions, every reported percentile within 0.1% of the exact sample percentile; bucket layout contiguous; merge equals single recording; merging differs from averaging |
| Coordinated-omission correction | `tests/unit/test_open_model.py`: schedule maths, pause shift, backlog; a simulated 0.5 s stall at 200 ops/s shows in response-time p90 (above 100 ms) but not in service-time p90 (below 50 ms) |
| Error classification | `tests/unit/test_errors.py`: code, code name and labels for NotWritablePrimary, PrimarySteppedDown, DuplicateKey, network classes, bulk write errors, redaction |
| Impact analysis on synthetic series | `tests/unit/test_analysis.py`: a 300-second series with a known disruption; asserts exact first and last failure times, failed count by class, 6,600 ms write gap, 400 ms read gap, primary change, baseline p99 2 ms, recovery 6 s, disruption detection bounds, client-bound rate and CPU rules, warm-up exclusion |
| Storage | `tests/unit/test_store.py`: 0600/0700 permissions, migrations with backup, refusal of a newer schema, interrupted runs keep data, retention, cross-process gap intersection |
| Server sampling without privileges | `tests/unit/test_monitor.py`: `serverStatus` and `replSetGetStatus` answering `Unauthorized` are recorded once each with the fix, never retried, while `hello` keeps being sampled |
| HTTP security | `tests/unit/test_server_security.py`: CSP and headers, custom header required, cross-origin requests blocked, schema validation, body size limit, password never returned, access gate and cookie flags, profile CRUD/export/import |

With `-W error`, a legacy test (`tests/legacy/test_observability.py`) installs the legacy app's global exception hook, which then logs to a closed stream at interpreter exit and prints `--- Logging error ---`. It does not fail any test and is in the legacy package, so it is left as is.

## 2. Integration tests against real replica sets

```
BALLAST_TEST_URI='mongodb://r81:27031,r82:27032,r83:27033/?replicaSet=rs0' \
BALLAST_TEST_MEMBERS='r81:27031,r82:27032,r83:27033' \
runtime/python/bin/python3 -m pytest -m integration tests/integration -v
```

| Test | What it proves |
|---|---|
| `test_failover_is_recorded_classified_and_measured[1]` and `[2]` | 400 ops/s with one and with two generator processes; steps down the primary between "stepdown started" and "stepdown finished" markers. Asserts topology events recorded for the stepped-down member, failures present and classified (`NotWritablePrimary` and similar), first and last failure times, primary change in the window, recovery time reported. |
| `test_write_stall_measures_gap_latency_and_recovery` | `db.fsyncLock()` blocks writes for 3 s. Asserts a write gap of at least 2.5 s, maximum response time of at least 2.5 s (the stall is measured as latency, not lost), baseline p99 under 0.5 s, recovery reported, and connection checkout waits recorded. |
| `test_repeated_runs_do_not_leak` | Four runs of two generator processes each in one manager. After them: no child processes, no new non-pool threads, file descriptors within 4 of the baseline, no extra asyncio tasks, no `ResourceWarning`. |
| `test_password_absent_everywhere` | Creates a database user with a known password, saves a target, tests a wrong password, runs a workload with sampling and a marker, then fetches run, runs, targets, system, samples, CSV, JSON and HTML exports and the log bundle, and greps the log directory. The password appears nowhere. |

Observed behavior worth knowing (from these runs, not assumptions):

- In this local Docker replica set an election after `rs.stepDown()` completes in well under 100 ms. With retryable writes on, a stepdown often shows no failures at all; the test turns retryable writes and reads off to make failures visible.
- During the write stall on 8.0, blocked updates held all 100 pooled connections, so reads waited for connections too (read gap 3.7 s in the browser run). On 7.0 the pool did not exhaust (checkout wait 18 ms). Ballast reports both as measured.
- Two-process failover first failed on 8.0 because the replica set script gave member 0 priority 2, so it retook the primary about 20 s after the stepdown and the run ended mid-disruption. The script now uses equal priorities (commit message "make integration tests independent of replica set priorities").

## 3. Browser end-to-end

```
BASE=https://127.0.0.1:8443/ BALLAST_TEST_URI='mongodb://r71:27021,r72:27022,r73:27023/?replicaSet=rs0' \
E2E_DISRUPT_CMD='docker exec r71 mongosh --port 27021 --quiet --eval "db.fsyncLock(); sleep(4000); db.fsyncUnlock()"' \
SHOTS=docs/design/after node tests/e2e/run.mjs
```

Steps: empty runs page; create a target through the form and see the connection test result; open a profile; walk the new-run wizard to the review; start the run and land on the live view; add "resize started" and "resize finished" markers with the M key while a 4-second write stall runs; check the active-run band on the runs page; pause and resume with P; stop with S and confirm; check the impact table; export the HTML report and open it from disk with network requests recorded; CSV and JSON exports; compare, settings, system pages; dark theme and compact density; keyboard shortcuts dialog and N. All passed, with no console errors and no request to any host other than the server. The exported report made zero network requests.

The resulting report (`docs/design/after/04-report.png`) shows the stall exactly as designed: a 4.07 s write gap, p99 rising from 6.8 ms before the window to 3.9 s during it, the open model's catch-up burst to about 1,500 ops/s as its backlog drained, and a recovery time of 10 s.

## 4. Accessibility

```
AXE=node_modules/axe-core/axe.min.js node tests/e2e/a11y.mjs
```

axe-core 4.10.2 with the `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa` and `wcag22aa` rule sets, on runs, new run, targets, profiles, compare, settings, system and a run report, in light and dark themes. The first scan found an `li` with `role="separator"` inside the navigation list and scrollable event lists without keyboard access; both were fixed and the final scan has 0 violations (contrast rules included). The live view is not in the automated scan because it needs an active run; it uses the same components. The scan needs Playwright's `bypassCSP`, because the strict CSP blocks the injected scanner script, which is itself a check that inline scripts cannot run.

At 1024 px (tablet landscape) the rail collapses to icons and no page scrolls horizontally (`scrollWidth` equals the viewport width on the report and new-run pages; screenshots `13-report-tablet-1024.png`, `14-new-run-tablet-1024.png`).

## 5. Soak test

```
bin/ballastctl start
runtime/python/bin/python3 tests/soak/soak.py --base http://127.0.0.1:9543 --uri 'mongodb://rs1:27011,rs2:27012,rs3:27013/?replicaSet=rs0' --seconds 7500 --pid <server pid> --out soak.csv
```

SOAK_RESULTS

## 6. Static checks

`make lint` (ruff check and format), `mypy ballast`, `tsc --checkJs` over the front end, and `sh -n` on every shell script: all clean (`docs/evidence/static-checks.txt`). `pip-audit -r requirements/runtime.lock`: no known vulnerabilities.

## 7. Simulated fresh host

```
scripts/simulate-fresh-host.sh dist/ballast-1.0.0-linux-amd64.tar.gz
```

The image (`scripts/droplet-sim/`) is Ubuntu 24.04 with only `curl` and `cron` added, which a stock droplet has, a normal user `bench`, and no `sudo`. It runs on a Docker network created with `--internal`, joined only by the replica set, so it has no route to the internet. The full transcript is `docs/evidence/fresh-host.log`. Step by step:

1. `curl https://pypi.org/` from inside fails: no internet. `command -v sudo` finds nothing.
2. The bundle is copied in and extracted as `bench`.
3. `config/ballast.yaml` sets `listen: 0.0.0.0:8443`, an access token file (mode 600), certificate paths outside the install directory (`/home/bench/mycerts/`), the container's address as a certificate host, and an HTTP redirect listener. `bin/ballast config validate` passes and `config print` shows each value with source `file`.
4. `bin/verify-host.sh --uri ...` (the same script you can run on a droplet): 23 passed, 0 failed. It covers running as a normal user, version, config validation, writable directories, `ballastctl start`, status exit 0, a second start not starting a second copy, health, HTTPS with the CSP header verified against the configured certificate, the listening port, the access gate rejecting a request without the token, saving a target, the connection test (MongoDB 8.0.34), starting a 20-second run, `ballastctl mark`, run completion, an offline HTML report of 261 KB, `kill -9` of the server with the supervisor restarting it under a new PID, `ballastctl stop`, and status exit 3 afterwards.
5. From outside the box: `https://<ip>:8443/api/health` returns 200; `http://<ip>:8080/` returns 308 to `https://<ip>:8443/`; curl verifying against the generated certificate returns 200; `openssl s_client -tls1_2` reports `Verify return code: 0 (ok)`.
6. Headless Chromium from outside the box is redirected to `/auth`, gets "That token is not correct." for a wrong token, and with the right token reaches the runs page with a session cookie that is HttpOnly, Secure and SameSite=Strict. (This step found that the gate's own form was blocked as cross-site, because `Referrer-Policy: no-referrer` makes browsers send `Origin: null`; fixed.)
7. A run is started and the server is killed with `kill -9` 15 seconds in. The supervisor restarts it; the run is marked `interrupted` with its 13 measured seconds and 2,601 operations kept and analysed.
8. `ballastctl autostart enable` finds no `systemctl --user` and installs a crontab `@reboot` entry. After `docker restart droplet`, `ballastctl status` reports running and `health` reports healthy, started by cron. (The simulated init clears `/run/crond.reboot`, as a real boot does with its tmpfs `/run`; without that, cron does not treat a container restart as a reboot.)
9. `autostart disable` removes the entry and `stop` stops it.

Not covered by the simulation: the systemd user unit path (no systemd in the container). The code path is in `ballast/autostart.py`; on a droplet `bin/ballastctl autostart enable` will try it first and report whether lingering is on.

## 8. Release bundles

```
make release
```

| Archive | Compressed size |
|---|---|
| `ballast-1.0.0-linux-amd64.tar.gz` | 93.5 MB |
| `ballast-1.0.0-linux-arm64.tar.gz` | 88.2 MB |
| `ballast-1.0.0-darwin-arm64.tar.gz` | 50.5 MB |

Each comes with a CycloneDX SBOM (`*.sbom.cdx.json`, also inside the bundle as `sbom.cdx.json`) and `THIRD_PARTY_NOTICES` generated from the shipped packages' metadata and license files, plus CPython, uPlot and IBM Plex. `dist/SHA256SUMS` lists the archives. The linux-amd64 bundle is the one tested in sections 5 and 7. The linux-arm64 and darwin-arm64 bundles were built and inspected but not executed here (no ARM host or Mac available); they contain the same files with platform wheels. `darwin-amd64` is not built (D-018).

Licensing of shipped components: no GPL, AGPL or SSPL package is included (`THIRD_PARTY_NOTICES`; audit section 7).

## 9. Other checks

- **Install from git**: a fresh `git clone` of the branch, `make build` (downloads the pinned Python, verifies its SHA-256, installs the lock file into `runtime/`), `bin/ballastctl start`, `health`, `stop`. `git status` is empty afterwards (`docs/evidence/git-install.txt`).
- **SIGHUP certificate reload**: a connection opened before `bin/ballast gen-cert` keeps working and keeps the old certificate; a new connection gets the new one; the log records the new fingerprint (`docs/evidence/sighup-reload.txt`).
- **Legacy dashboard**: `bin/ballast legacy run --help` exits 2 with an explanation while `features.legacy_dashboard` is false; with the flag it runs, and a headless legacy run against MongoDB 7.0 completes with non-zero progress counts, showing fix B6 (`docs/evidence/legacy-flag.txt`).
- **Data store migration**: covered by `tests/unit/test_store.py::test_migration_from_older_version_takes_backup` (backup taken before migrating, schema version advanced) and `test_newer_schema_is_refused`.

## Not verified

- A real DigitalOcean droplet. Run `bin/verify-host.sh --uri '<connection string>'` there.
- The systemd user unit autostart path (no systemd in the simulation).
- Execution of the ARM64 and macOS bundles.
- Server sampling against a cluster with authentication enforced. The local replica sets run without `--auth`, so the "privileges missing" path is covered by a unit test with a stub client only, and the password test exercises redaction, not server-side authentication.
- Behavior against sharded clusters and MongoDB Atlas; the tool connects to them, but the tests used replica sets only.
