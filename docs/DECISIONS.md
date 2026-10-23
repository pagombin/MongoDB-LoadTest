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

## Blocked

(none yet)
