# Morning report, 2026-10-23

Branch `claude/lucid-goodall-0s1ayr`. No pull request has been opened. Every pushed commit builds and passes its tests. Evidence for each claim is in `docs/VERIFICATION.md` and `docs/evidence/`.

## Completed

| What | Commit |
|---|---|
| Audit of the old app, decisions log, before screenshots | [8a6e9e3](https://github.com/pagombin/MongoDB-LoadTest/commit/8a6e9e3) |
| Design plan (tokens, type, wireframes for 9 screens) | [23321f3](https://github.com/pagombin/MongoDB-LoadTest/commit/23321f3) |
| Eight measurement bugs in the old engine fixed (batch latency divided by batch size, and others) | [a76ffde](https://github.com/pagombin/MongoDB-LoadTest/commit/a76ffde) |
| Measurement core: HDR-layout histograms, error classes, impact analysis | [f0085fb](https://github.com/pagombin/MongoDB-LoadTest/commit/f0085fb) |
| Load engine: open and closed models, generator processes, driver events, local SQLite store, markers | [a87d355](https://github.com/pagombin/MongoDB-LoadTest/commit/a87d355) |
| Server, configuration, HTTPS, access token, supervisor and `ballastctl` | [2485f65](https://github.com/pagombin/MongoDB-LoadTest/commit/2485f65) |
| New UI: runs, new run, live, report, compare, targets, profiles, settings, system | [2e6b1d7](https://github.com/pagombin/MongoDB-LoadTest/commit/2e6b1d7) |
| Unit and integration tests (failover under load, write stall, password leakage) | [451541e](https://github.com/pagombin/MongoDB-LoadTest/commit/451541e), [cf3f33a](https://github.com/pagombin/MongoDB-LoadTest/commit/cf3f33a) |
| Release bundles, CI, docs, browser end-to-end test, after screenshots | [30db362](https://github.com/pagombin/MongoDB-LoadTest/commit/30db362) |
| Accessibility fixes, fresh-host simulation, leak and SIGHUP checks, MongoDB 7.0 evidence | [b8b41bd](https://github.com/pagombin/MongoDB-LoadTest/commit/b8b41bd), [4f202ab](https://github.com/pagombin/MongoDB-LoadTest/commit/4f202ab) |
| Independent code review: 6 bugs and 3 smaller issues found and fixed, with tests | [6038045](https://github.com/pagombin/MongoDB-LoadTest/commit/6038045), [36985ec](https://github.com/pagombin/MongoDB-LoadTest/commit/36985ec) |
| Verification report, soak result, this report | see the last commits on the branch |

Results: 148 unit tests, 5 of 5 integration tests on both MongoDB 7.0 and 8.0, 17 of 17 browser checks, 0 accessibility violations, 0 lint or type errors, no known vulnerabilities in 53 pinned packages, 23 of 23 checks on a simulated fresh host with no internet and no sudo. The 2-hour soak is still running; its result will be added here.

Screenshots of every page are in `docs/design/after/`, with the old app in `docs/design/before/`.

## Not completed or not verified

- Not run on a real droplet, an ARM host or a Mac. The linux-arm64 and darwin-arm64 bundles were built and inspected only.
- The systemd user unit for autostart was not exercised (no systemd in the container). The crontab method was.
- Server sampling against a user without `clusterMonitor` was tested with a stub, not a real cluster with auth.
- Not tested against sharded clusters or Atlas.
- Features of the old app not ported to the new engine (multi-host leader/follower, Schema Builder) stay in the legacy dashboard behind a flag (D-003).

## Review first

- **D-002**: kept Python and replaced Dash with a plain HTML/JS front end, instead of a Go rewrite.
- **D-003**: the old dashboard is kept, off by default, as `ballast legacy`.
- **D-006**: how marker labels pair into impact windows ("... started" with "... finished").
- **D-027**: self-signed certificates are now generated in pure Python, so `cryptography` is no longer needed. Tested against `ssl`, `cryptography` and `openssl`, but not externally reviewed.
- **D-023**: one commit accidentally added about 250 MB of downloads, removed in the next commit. **Merge with squash** so the default branch never contains them.

## Blocked

Nothing.

## Commands

Build the release bundles (written to `dist/`):

```
make build
make release
```

Start from a bundle on the server:

```
tar -xzf ballast-1.0.0-linux-amd64.tar.gz
cd ballast-1.0.0
bin/ballastctl start
bin/ballastctl status
```

See the UI from your laptop. Ballast listens on 127.0.0.1:8443 by default, so tunnel to it:

```
ssh -L 8443:127.0.0.1:8443 you@server
```

Then open https://127.0.0.1:8443 and accept the self-signed certificate. `bin/ballastctl start` prints its SHA-256 fingerprint for comparison.
