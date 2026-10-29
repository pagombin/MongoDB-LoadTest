# Decisions log

Each entry: the question, the options considered, the choice, why, and how hard it is to reverse. Entries marked **Review first** are the ones I am least sure about.

## D-001 Branch name

- Question: the brief asks for `pagombin/enterprise-redesign`; this session is configured to develop and push only on `claude/lucid-goodall-0s1ayr`.
- Options: (a) push to the brief's branch name; (b) push to the session branch.
- Choice: (b). The session's git rules forbid pushing to another branch without explicit permission.
- Reverse: trivial. `git push origin claude/lucid-goodall-0s1ayr:pagombin/enterprise-redesign`.

## D-002 Keep Python, replace the Dash UI — **Review first**

- Question: rewrite the backend in Go, or keep Python?
- Options: (a) Go rewrite with embedded assets; (b) keep Python, ship a relocatable CPython plus vendored wheels; (c) keep Python and Dash unchanged.
- Evidence: the bundle is possible in Python (`docs/AUDIT.md` section 5.3: python-build-standalone reachable, wheels downloadable per platform, Chrome dependency removable). Brief rule 2.4 allows a rewrite only when no smaller fix works.
- Choice: (b). The backend stays Python. The Dash front end is replaced by a static HTML/JS/CSS front end with no build step, served by `aiohttp`, because the brief requires server-sent events or WebSockets (7.3.7), a strict CSP (11.7), designed empty/loading/error states, and keyboard operation, none of which Dash's single polling callback provides.
- Known cost: Python's GIL limits one generator process to roughly 5-15K ops/s. Mitigated by running load in separate generator processes (D-009) and by client-bound detection.
- Reverse: hard. A Go port later would reuse the API, storage schema and front end unchanged, because the front end only talks to the HTTP API.

## D-003 Legacy dashboard kept behind a flag — **Review first**

- Question: the old Dash app has features the new engine does not port tonight (multi-host leader/follower mode, Schema Builder modal, per-collection schema overrides edited in the UI). Brief rule 2.3 forbids deleting features.
- Options: (a) delete; (b) port everything tonight; (c) keep the package as is, fix its critical bugs, expose it as `ballast legacy run -c <file>` only when `features.legacy_dashboard: true`.
- Choice: (c). Off by default because it writes to `loader_metadata` on the cluster under test (B9) and serves plain HTTP.
- Reverse: easy. Flip the flag; or delete `mongo_load_tester/` when the new engine covers everything.

## D-004 One benchmark database, N collections

- Question: legacy spreads load over N databases; brief 5.6.1 says the tool writes only to a database the user names.
- Choice: new profiles name one database and a collection count. Legacy keeps its N-database behavior behind its flag.
- Reverse: moderate (profile schema change plus migration).

## D-005 How collections are tagged

- Options: (a) a naming prefix only; (b) a registry collection `_ballast_collections` in the benchmark database listing every collection the tool created, with product name and creation time; (c) a validator or comment on each collection.
- Choice: (b), plus the naming prefix. Cleanup drops only names found in the registry that still exist, then drops the registry. Works on managed services where `collMod` may be restricted.
- Reverse: easy.

## D-006 How marker pairs become impact windows — **Review first**

- Choice: a marker has an optional `phase` of `begin` or `end`. If not given, it is inferred from the label: a label ending in `start`, `started`, `begin`, or `began` is a begin; one ending in `finish`, `finished`, `end`, `ended`, `done`, `complete`, or `completed` is an end. A begin pairs with the next end. Any two markers can also be analysed on demand in the report. Automatically detected disruptions are analysed too.
- Reverse: easy (analysis only, stored markers unchanged).

## D-007 PNG and PDF export without Chrome

- Question: legacy PNG/PDF need Kaleido, which needs Google Chrome (B8).
- Choice: PNG is saved from each chart in the browser (the charts are canvas). PDF is produced by printing the offline HTML report, which has a print stylesheet. Server-side Kaleido and ReportLab are dropped from the new product; the legacy app keeps them.
- Reverse: easy.

## D-008 License placeholder

- Choice: `LICENSE` reads "Copyright Paul Agombin. All rights reserved." as the brief asks. No open-source license is added.
- Reverse: trivial.

## D-009 Load generation runs in a child process

- Question: run the workload in the server process, in threads, or in separate processes?
- Choice: each run starts one or more generator processes (default 1, profile setting `generator.processes`). Each uses PyMongo's async client and an asyncio loop. They send one report per second to the server process over a pipe. The web server, SSE, storage and analysis never compete with measurement for the same GIL.
- Reverse: moderate.

## D-010 Histogram implementation

- Options: (a) `hdrhistogram` package; (b) own log-linear histogram with HDR's bucket layout.
- Choice: (b). 3 significant digits (relative error at most 1/1024 of the value, under 0.1%), values in microseconds from 1 us to 1 hour, sparse storage so per-second histograms stay small. Easy to merge, serialize, and test, no compiled dependency.
- Reverse: easy; the histogram is one module.

## D-011 Local storage is SQLite

- Choice: `data/ballast.db` (SQLite, WAL mode, file mode 0600, directory 0700). Stdlib, public domain, no server. Per-second data is committed every second, so a crash loses at most one second.
- Reverse: moderate.

## D-012 Open model scheduling

- Choice: fixed-interval schedule (intended start of operation i is `t0 + i / rate`), executed by a bounded pool of async workers. Only the next index is stored, so a stalled cluster grows latency, not memory. Pausing shifts `t0`. Changing the rate rebases `t0` at the current index. This is the method used by wrk2.
- Reverse: easy (Poisson arrivals could be added as an option).

## D-013 Server names product as Ballast in one file

- Choice: `ballast/product.py` holds name, short description, and version. Everything else imports from there.

## D-014 Front end has no build step

- Choice: plain ES modules, CSS and vendored uPlot (MIT), checked with `tsc --checkJs` in CI. The git install path then needs no Node.js.
- Reverse: easy.

## D-015 Fonts

- Choice: IBM Plex Sans and IBM Plex Mono (SIL OFL 1.1), latin subset, woff2, vendored from the `@fontsource` packages.

## D-016 Environment variable naming

- Question: keys contain underscores, so `BALLAST_SERVER_ACCESS_TOKEN_FILE` is ambiguous in general.
- Choice: the first segment after `BALLAST_` is the section (section names have no underscores), the rest is the key: `BALLAST_SERVER_ACCESS_TOKEN_FILE` sets `server.access_token_file`. Unknown `BALLAST_*` variables stop startup, except `BALLAST_HOME` and `BALLAST_CONFIG`.
- Reverse: easy.

## D-017 Bundle top-level directory and contents

- Question: the brief's layout starts at `ballast/`, but upgrades extract the new bundle next to the old one, which would collide.
- Choice: archives unpack to `ballast-<version>/` with the brief's layout inside, plus `runtime/python` (private CPython 3.12 with all dependencies in its site-packages), `LICENSE`, `README.md`, `sbom.cdx.json`, and `bin/verify-host.sh`.
- Reverse: easy (archive naming only).

## D-018 macOS bundles (resolved by D-027)

- Question: the brief asks for a darwin bundle for local development.
- Evidence: `pip download --platform macosx_*_x86_64 cryptography==50.0.2` fails; the newest Intel macOS wheel is 48.0.1.
- First choice: build `darwin-arm64` only.
- Update: since D-027 removed `cryptography`, `darwin-amd64` builds from the same lock and is a default target. Its interpreter and compiled extensions were checked to be x86-64 Mach-O; it was not run on a Mac.

## D-019 Legacy dependencies ship without Kaleido

- Choice: the bundle includes Dash, Plotly, ReportLab, Typer and Waitress so `ballast legacy` works offline, but not Kaleido, which needs Google Chrome at runtime anyway (B8). Legacy PNG and PDF buttons keep failing as before; the new report covers both (D-007).
- Reverse: easy.

## D-020 Debug symbols stripped from Linux bundles

- Evidence: `libpython3.12.so.1.0` in python-build-standalone is 200 MB with symbols; the stripped linux-amd64 bundle is 65.6 MB compressed.
- Choice: `strip --strip-unneeded` on shared objects (cross binutils for arm64 at build time). macOS binaries are left as shipped.

## D-021 Autostart writes outside the install directory

- Question: brief 10.1.1 says no writing outside the extracted directory, but 10.3 asks for a systemd user unit or crontab entry, which by nature live outside it.
- Choice: only `ballastctl autostart enable` writes outside the install directory (`~/.config/systemd/user/ballast-<hash>.service` or the user crontab), and `autostart disable` removes it. Nothing else does.

## D-022 Referrer-Policy is same-origin, not no-referrer

- Question: brief 11.7 asks for a `Referrer-Policy` header without naming a value.
- Evidence: with `no-referrer`, browsers send `Origin: null` on HTML form posts, so the access-gate form was rejected by the same-origin check (found in the fresh-host test, `docs/VERIFICATION.md` section 7).
- Choice: `same-origin`. No referrer is ever sent to another site; the gate also accepts `Origin: null` only when `Sec-Fetch-Site: same-origin`.
- Reverse: easy.

## D-023 Download cache in the branch history — **Review first**

- What happened: commit "Add packaging, CI, docs, e2e browser test and after screenshots" accidentally included `.cache/` (Python builds and wheels, about 250 MB). The next commit removed it and ignored the directory. The brief forbids rewriting history, so the blobs stay in this branch's history.
- Recommendation: merge the pull request with **squash**, so the default branch never contains them. If the branch is merged without squashing, the repository grows by about 250 MB.
- Reverse: only by rewriting history, which needs your approval.

## D-024 Default load model and profiles

- Choice: new installs get three profiles: "OLTP steady 1k" (open model, the default in New run), "Write heavy 2k" (open), and "Closed loop 32 workers". New run picks the first open-model profile, because scale testing needs a fixed rate (`docs/SCALE-TESTING.md`).
- Reverse: easy.

## D-025 Test environment substitutions

- The brief asks for integration tests against a three-member replica set "started locally (Docker Compose or downloaded mongod binaries)". `fastdl.mongodb.org` was blocked here (HTTP 403 from the egress proxy), so the sets run from the official `mongo:7.0` and `mongo:8.0` Docker images with `scripts/dev-replset.sh`. CI uses the same script.
- The simulated droplet image adds `curl` and `cron` from `.deb` files downloaded on the build host, because package installs inside `docker build` had no network path here (`scripts/droplet-sim/README.md`).

## D-026 Generator processes per run

- Choice: default 1 generator process; profiles can set up to 64. One process ran 400 ops/s at about 27% of one core in the browser test (`docs/design/after/03-live.png`, "Generator CPU"). Client-bound detection tells you when to add more.

## D-027 Self-signed certificates in pure Python — **Review first**

- Question: `cryptography` was the only compiled dependency that is hard to install on locked-down hosts, and it was used only to make and read the self-signed certificate.
- Options: (a) keep it required; (b) call the `openssl` command when present; (c) generate RSA-2048 keys and DER-encode the certificate in Python.
- Choice: (c), in `ballast/tls.py` (about 250 lines). RSA-2048 with SHA-256, SAN, key usage, server-auth EKU, 397-day validity. Certificates from files (`tls.mode: files`) can be any key type; only their fields are read.
- Evidence: `tests/unit/test_tls.py` completes a TLS handshake with Python's `ssl`, verifies the signature with `cryptography`, and parses a certificate made by `cryptography`; `openssl x509 -text`, `openssl rsa -check` and `openssl verify` accept the output; Chromium loads the UI over it in the browser test.
- Cost: an RSA key takes about 1 s to generate instead of milliseconds for EC; this happens once per certificate. Key generation uses `secrets` (the OS random source); the code has not had an external cryptography review.
- Reverse: easy; the module keeps its interface.

## D-028 Built-in reader for the YAML subset the config uses

- Question: PyYAML is usually installable, but not always.
- Choice: use PyYAML when present; otherwise `ballast/miniyaml.py` reads mappings, lists, flow collections, scalars with YAML 1.1 resolution (so `off` is false, as with PyYAML), comments and block text, and refuses anchors, tags and multiple documents with a message naming PyYAML.
- Evidence: `tests/unit/test_miniyaml.py` compares it with PyYAML on the shipped example and on edge cases.
- Reverse: easy.

## D-029 Python 3.10 floor

- Question: Ubuntu 22.04 and other common servers ship Python 3.10; the code required 3.11.
- Choice: `requires-python >= 3.10`. Only `datetime.UTC` needed replacing. Unit tests pass on 3.10 with only the three required packages.
- Reverse: easy.

## D-030 The pip installer copies Ballast instead of building it

- Question: on Ubuntu 22.04, `pip install --target` with pip 22 builds packages with the system setuptools 59, which ignores `pyproject.toml` and installs an empty `UNKNOWN` package.
- Choice: `scripts/install.sh` installs only dependencies with pip and copies the pure-Python `ballast` and `mongo_load_tester` packages into `runtime/site`. `bin/ballast` starts Python with `-I` through `bin/ballast-launch.py`, so the current directory and `PYTHON*` variables cannot change what is imported.
- Reverse: easy.

## D-031 Guardrails and SLO criteria are off unless set

- Choice: profiles start with no guardrail and no criteria, because sensible limits depend on the cluster. The wizard shows both in the Schedule step. Guardrails count only measured seconds, so warm-up and data load cannot trip them.
- Reverse: easy.

## D-032 `/metrics` on by default

- Choice: Prometheus metrics are served by default (`features.prometheus_metrics: true`), behind the same access gate as everything else, and the server listens on 127.0.0.1 by default. They expose run names and error classes, nothing secret.
- Reverse: set the flag to false.

## D-033 One schedule per read route — **Review first**

- Question: with aggregations routed to analytics nodes and everything else on the primary, an unavailable analytics node made the waiting aggregations take all in-flight slots. Point reads on the primary then showed 8 s p99, with 31 ms on the wire.
- Options: (a) one shared pool of slots, as before; (b) one schedule and slot pool per route, in proportion to its share; (c) one generator process per route.
- Choice: (b), on by default when read routing is set (`load.isolate_routes`). It models an application and a BI tool as separate clients, which is how they run against Atlas. Both still share the driver's connection pool, as they would in one process; waiting for server selection holds no connection.
- Evidence: the integration test now requires primary point reads to keep p99 under 1 s while the analytics node restarts; it was 7.96 s before.
- Reverse: easy (`isolate_routes: false`).

## D-034 Read preference comes from the connection string unless the profile sets one

- What changed: profiles used to send `readPreference=primary` always, which overrode the connection string. An Atlas analytics or read-only connection string (`readPreference=secondary&readPreferenceTags=nodeType:ANALYTICS`) made every run fail with "Read preference primary cannot be combined with tags", and `readPreference=secondary` alone was silently ignored.
- Choice: `driver.read_preference` is empty by default and then the connection string decides. Saved profiles that set `primary` explicitly keep doing so.
- Reverse: easy.

## D-035 An access token is created automatically when listening beyond loopback

- What changed: with `server.listen` set to a non-loopback address and no token configured, the server used to start open to anyone who could reach the port, with only a warning. A user reaching the UI through a jump server had to create the token by hand.
- Choice: at startup, create `config/token` (32 URL-safe characters, mode 600, kept across restarts) and require it. `ballastctl start` prints it, `ballastctl token` shows it, and `ballastctl token --new` replaces it. The token is never written to the logs. `server.auto_access_token: false` turns this off when something in front of Ballast already controls access. Listening on loopback is unchanged: no token unless one is set.
- Reverse: easy (set the default of `server.auto_access_token` to false).

## Blocked

(none yet)
