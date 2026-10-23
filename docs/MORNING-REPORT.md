# Morning report, 2026-10-23

Branch `claude/lucid-goodall-0s1ayr`. No pull request has been opened. Evidence for every claim is in `docs/VERIFICATION.md` and `docs/evidence/`; the 31 enhancements are listed one by one in `docs/ENHANCEMENTS.md`. One pushed commit (2f42758) failed the format check and was fixed by the next one (61e1ad0); its tests passed.

## Completed

| What | Commit |
|---|---|
| Audit, decisions log, design plan, before screenshots | [8a6e9e3](https://github.com/pagombin/MongoDB-LoadTest/commit/8a6e9e3), [23321f3](https://github.com/pagombin/MongoDB-LoadTest/commit/23321f3) |
| Measurement core, load engine, server, HTTPS, supervisor, new UI | [f0085fb](https://github.com/pagombin/MongoDB-LoadTest/commit/f0085fb) to [2e6b1d7](https://github.com/pagombin/MongoDB-LoadTest/commit/2e6b1d7) |
| Tests, packaging, CI, docs, accessibility and fresh-host checks | [451541e](https://github.com/pagombin/MongoDB-LoadTest/commit/451541e) to [b8b41bd](https://github.com/pagombin/MongoDB-LoadTest/commit/b8b41bd) |
| Code review: 6 bugs and 3 smaller issues fixed | [6038045](https://github.com/pagombin/MongoDB-LoadTest/commit/6038045), [36985ec](https://github.com/pagombin/MongoDB-LoadTest/commit/36985ec) |
| Runs with only `aiohttp`, `pydantic` and `pymongo` on Python 3.10+; no `cryptography`; `ballast doctor` | [36a2184](https://github.com/pagombin/MongoDB-LoadTest/commit/36a2184) |
| Installer for hosts without venv, offline kit | [ca98107](https://github.com/pagombin/MongoDB-LoadTest/commit/ca98107), [039bea5](https://github.com/pagombin/MongoDB-LoadTest/commit/039bea5) |
| Guardrails, SLO, `ballast run` for CI, webhooks, `/metrics`, tags and notes, rerun, Markdown, backup and restore | [cad7be8](https://github.com/pagombin/MongoDB-LoadTest/commit/cad7be8) |
| UI for all of the above, progress bar, command palette, compare deltas | [661f4dc](https://github.com/pagombin/MongoDB-LoadTest/commit/661f4dc) |
| Long-run memory fix found by the soak (over 1 GB → 160 MB for a 2-hour run) | [2f42758](https://github.com/pagombin/MongoDB-LoadTest/commit/2f42758) |
| Soak results, docs, bundles for 4 platforms | [f37c875](https://github.com/pagombin/MongoDB-LoadTest/commit/f37c875), [27a593e](https://github.com/pagombin/MongoDB-LoadTest/commit/27a593e) |
| Atlas analytics and read-only nodes: routing, per-member figures, lag, route gaps; fixes the connection-string read preference being overridden | [f1e8570](https://github.com/pagombin/MongoDB-LoadTest/commit/f1e8570), [c462e02](https://github.com/pagombin/MongoDB-LoadTest/commit/c462e02) |

Results on the final code:
- **Unit tests:** 202 pass. 93 of them also pass on Python 3.10 with only the core packages installed.
- **Integration:** 7 of 7 on MongoDB 7.0 and on 8.0.
- **Browser:** 20 of 20 checks pass.
- **Accessibility:** 0 violations across 22 page, dialog and theme combinations.
- **Code checks:** 0 lint or type errors, and no known vulnerabilities in 50 pinned packages.
- **Installs:** 23 of 23 checks on a simulated fresh host. Ubuntu 22.04 without `python3-venv` installs and passes 21 of 21 checks, and the offline kit installs with no network.
- **Soak:** 2 hours at 1,000 ops/s. Memory and file descriptors stayed flat once the live-chart buffer filled.

Screenshots of every page: `docs/design/after/` (21 images). The old app: `docs/design/before/`.

## Not completed or not verified

- Not run on a real droplet, an ARM host or a Mac. The linux-arm64 and both macOS bundles were built and inspected only.
- The systemd user unit for autostart was not exercised (no systemd in the container). The crontab method was.
- The installer's pure-Python retry for aiohttp was not exercised, because every test host had wheels.
- Desktop notifications were checked by hand only. Sampling without `clusterMonitor` was checked with a stub only.
- Not tested against sharded clusters or a real Atlas cluster. Analytics and read-only node support was tested on a local replica set tagged the way Atlas tags them.
- Two 7.0 integration tests failed once each and passed every rerun (`docs/VERIFICATION.md` section 2); the cause is not established.
- The soak ran on the bundle from before the memory fix. The fix was measured on a copy of the soak's database, not with a second 2-hour soak.

## Review first

- **D-002**: kept Python and replaced Dash with a plain HTML/JS front end, instead of a Go rewrite.
- **D-003**: the old dashboard is kept, off by default, as `ballast legacy`.
- **D-006**: how marker labels pair into impact windows ("... started" with "... finished").
- **D-023**: one commit accidentally added about 250 MB of downloads, removed in the next commit. **Merge with squash** so the default branch never contains them.
- **D-033**: with read routing, each route gets its own schedule and in-flight slots, so a stalled analytics node cannot make primary traffic look slow.
- **D-027**: self-signed certificates are now generated in pure Python, so `cryptography` is no longer needed. Tested against `ssl`, `cryptography`, `openssl` and Chromium, but not externally reviewed.

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
```

Or, with the server's own Python 3.10+ and pip, from a checkout:

```
sh scripts/install.sh --prefix ~/ballast
~/ballast/bin/ballastctl start
```

See the UI from your laptop. Ballast listens on 127.0.0.1:8443 by default, so tunnel to it:

```
ssh -L 8443:127.0.0.1:8443 you@server
```

Then open https://127.0.0.1:8443 and accept the self-signed certificate. `bin/ballastctl start` prints its SHA-256 fingerprint for comparison.
