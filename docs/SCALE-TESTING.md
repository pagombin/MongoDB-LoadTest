# Scale testing with Ballast

This walkthrough measures what an application would experience while you resize a replica set or change its members. You trigger the scale operation yourself (Atlas, Terraform, a cloud console, `rs.reconfig`); Ballast measures.

## 1. Prepare

- Run Ballast on a host in the same region and network path as the application you care about.
- Create a database user for benchmarking with `readWrite` on the benchmark database. Add `clusterMonitor` if you want member state and replication lag samples; it is optional.
- Add the cluster under **Targets** and press **Test connection**. Check that it sees every member and the primary you expect.

## 2. Choose a workload

For scale tests use the **open model**: a fixed rate that does not slow down when the database does. Then a stall shows up as latency and failures instead of disappearing (see `docs/METHODOLOGY.md`).

Pick a rate your cluster handles comfortably now, typically the production peak or a fraction of it. The built-in profile **OLTP steady 1k** (60% reads by key, 30% updates, 10% inserts at 1,000 ops/s) is a reasonable start.

Settings worth deciding before you start:

| Setting | Why it matters during scale operations |
|---|---|
| Retryable writes and reads | On (the driver default) hides short elections behind retries; off shows every failed attempt. Match your application. |
| Write concern | `majority` shows the cost of losing a secondary; `1` does not. |
| `serverSelectionTimeoutMS` | How long an operation waits for a new primary before failing. The default 30 s turns an election into latency; a short value turns it into failures. |
| Operation timeout (`timeoutMS`) | Puts an upper bound on every operation, like many applications do. |
| Max in flight | Must be large enough to absorb a stall: rate × expected stall length. At 1,000 ops/s and a 10 s election, 64 slots fill in 64 ms, after which the backlog grows and is measured as latency. |
| Data load | Load enough documents that the working set resembles production. |
| Steady duration | "Until I stop it" is easiest for scale tests. |

Two optional settings matter when the cluster serves real traffic:

- **Guardrails** stop the run by itself when more than a given share of operations fail, or p99 stays above a limit, for a number of measured seconds in a row (10 by default). Use them when testing against production, so a bad change does not keep adding load. A guardrail stop is shown on the report with its reason, and everything up to the stop is kept.
- **Pass/fail criteria** (SLO) are checked against the whole measured run: p50, p99 and max response time, share of failed operations, throughput, and share of the target rate achieved. The report shows a verdict per criterion, the runs list has an SLO column, and `bin/ballast run` exits with code 1 when one fails.

Both are in the profile and in the **Schedule** step of **New run**.

## 3. Start the run and wait for a baseline

Start the run from **New run**. Let it reach the steady phase and run for at least the baseline length (60 seconds by default) before you change anything, so the impact analysis has a clean "before" to compare against.

Check that the run is not flagged **client-bound** on the live view. If it is, the generator is the bottleneck; add generator processes or reduce the rate before going further.

## 4. Mark the scale operation

Add a marker when the change starts and another when it finishes. Labels ending in "started" and "finished" (or "start"/"end", "begin"/"done") pair up automatically into an impact window.

From the UI, press **M**, type the label, and press Enter.

From a script on the Ballast host, put markers next to the command that triggers the change:

```
bin/ballastctl mark "resize started" --note "M40 to M50"
atlas clusters update prod --tier M50
atlas clusters watch prod
bin/ballastctl mark "resize finished"
```

From another machine, call the API (with the access token if one is configured):

```
curl --cacert ballast.crt -H "Authorization: Bearer $TOKEN" -H 'X-Ballast-Request: 1' \
  -H 'Content-Type: application/json' -d '{"label":"resize started"}' \
  https://ballast.example.com:8443/api/runs/active/markers
```

You can add markers with an explicit phase when the label does not say: `--phase begin` / `--phase end`, or `"phase": "begin"` in the API.

During a rolling resize you can also mark each member: `bin/ballastctl mark "rs2 restarted"`. Point markers without a phase appear on the timeline but do not form windows.

## 5. Watch

The live view shows throughput against the target, p50/p99/max response time on a log scale, and a timeline lane under the charts with markers, topology events from the driver (member state changes, primary changes, heartbeat failures), failed operations, connection pool clears and write/read gaps. The right-hand panels show errors by class and the replica set members.

Leave the run going after the "finished" marker until throughput and p99 are back to normal for a while, so recovery can be measured. A minute is usually enough.

## 6. Read the report

Stop the run (**S**, or `Stop run`). The report's **Impact analysis** table has one row per window:

| Column | Meaning |
|---|---|
| First / last failure | Exact time of the first and last failed operation in the analysis span. |
| Failed ops | Count, with the breakdown by error class under **Details**. |
| Write gap / Read gap | The longest interval with no successful write / read. This is how long the application could not write. |
| Primary changes | Elections observed by the driver, with times under **Details**. |
| p99 before / during / after | p99 response time in the baseline, during the window, and after it. |
| Recovery | Seconds from the end marker until throughput and p99 stayed within tolerance of the baseline for 5 seconds. "not within run" means it never did before you stopped. |

**Details** adds p50, p99, max and throughput for before/during/after, the exact gap intervals, and the list of primary changes.

Disruptions Ballast detected on its own (failures, throughput below half of normal, p99 above four times normal) get rows too, marked "detected". One inside your marker window confirms what you saw; one outside it is worth a look.

To analyse a range without markers, zoom the charts to it (drag across a chart) and press **Analyze visible range**.

## 7. Compare and share

- **Run again** on the report starts the same workload against the same target, so a before/after pair takes one click. Notes and tags help find runs later: add them on the report, and filter the runs list by tag. Filtered lists keep their filters in the address, so you can bookmark or share them.
- **Copy summary** puts a Markdown summary on the clipboard (headline numbers, pass/fail verdict, one line per impact window) for a ticket or a chat message. **Markdown** downloads the same text.
- **Compare with...** puts runs on the same axes, aligned at their start or at a marker such as "resize started", so you can compare two resize methods or two driver settings. The summary table shows each run's change against the first one, green when better and red when worse.
- **Export HTML** gives a single file with the charts, impact table and configuration snapshot. It works offline and prints to PDF.
- **CSV** and **JSON** contain every second, with full histograms in the JSON.

## Atlas analytics and read-only nodes

Atlas tags every member of a cluster: `nodeType:ELECTABLE` for the members that can become primary, `nodeType:ANALYTICS` for analytics nodes and `nodeType:READ_ONLY` for read-only nodes. Analytics nodes have their own tier, so you can resize them separately; read-only nodes are usually in other regions. Both have priority 0 and no vote. Ballast reads these tags from the driver and shows them next to every member, on the target's connection test, in the live view and in the report.

**Choose where reads go.** In **New run**, step Workload, **Where reads go**:

| Choice | Reads go to | Use it to measure |
|---|---|---|
| From connection string | whatever `readPreference` and `readPreferenceTags` in the target's connection string say, else the primary | an application that connects with an Atlas analytics or read-only connection string |
| Atlas analytics nodes | `secondary` members tagged `nodeType:ANALYTICS` | a resize of the analytics tier, or analytics nodes being added or removed |
| Atlas read-only nodes | `secondary` members tagged `nodeType:READ_ONLY` | read-only node changes, and replication lag to other regions |
| Custom tags | your tag sets, tried in order; an empty set means any secondary | other tag layouts |

Choose **All reads**, or **Aggregations and analytics queries only** to keep point reads and writes on the primary while aggregations go to the analytics nodes. That is the usual Atlas pattern: an application on the primary, a BI tool on the analytics nodes. The built-in profile **Atlas analytics nodes** does this, with the **Analytics query** operation (an aggregation that scans 5,000 documents, groups and sorts them; change the size in the profile). **Atlas read-only nodes** sends all reads to read-only nodes.

When reads are split like this, each route gets its own schedule and its own in-flight slots in proportion to its share of the operations, as separate applications would. Without that, analytics queries waiting for an unavailable node would take every slot, and the primary's operations would show seconds of latency they never had. Set `load.isolate_routes: false` in the profile to share one pool of slots instead.

**Before you start**, the review step lists the members each kind of read can reach. If a route matches no member, for example because the connection string asks for `nodeType:ANALYTICS` and the cluster has no analytics nodes, it says so: those reads would wait `serverSelectionTimeoutMS` and fail.

**Decide what an unavailable node should do.** With only one analytics node, a resize restarts it and no member matches for a while:

- With the default `serverSelectionTimeoutMS` of 30 s, analytics queries wait and then succeed. The report shows no failures, but long analytics latency and a gap on the analytics route.
- With a short timeout (Driver settings, `serverSelectionTimeoutMS`), they fail with `ServerSelectionTimeout`. These failures never reach a member, so they count under the operation, not under a member.
- A fallback tag set (custom tags `nodeType:ANALYTICS; ` with an empty second set) lets reads move to any secondary while no analytics node is available. Measure that if your BI tool is configured that way.

Match what your applications use.

**Read the report.** Next to the usual figures:

| Where | What it shows |
|---|---|
| Members panel | per member: node type, reads, writes and failures it answered, p50/p99 service time on the wire, the largest replication lag seen, and how long it was unreachable; a chart of operations per member over time |
| Impact window, Gaps | a **Longest ANALYTICS read gap** (one row per route) next to the overall read and write gaps |
| Impact window, Members | reads per second each member answered before, during and after the window, failures on it, its largest lag, and seconds seen down |
| Impact window, Failed by operation | which operations failed, for example only the analytics queries |
| Impact window, Primary and member changes | member state changes with their node type, members added and removed |

Replication lag comes from the driver's own heartbeats (the same inputs it uses for `maxStalenessSeconds`), so it needs no `clusterMonitor` role. It is accurate to about half a second.

**Verified locally, not on Atlas.** `scripts/dev-replset-atlas.sh` starts a five-member replica set tagged the way Atlas tags clusters. `tests/integration/test_atlas_nodes.py` restarts its analytics node during a run, with short and default selection timeouts. In both cases the analytics route shows a gap of about 11 s, analytics queries fail or wait, and point reads and writes on the primary are unaffected (p99 under 1 s, no write gap). It also blocks replication on the read-only node and checks the lag appears. Ballast has not been run against a real Atlas cluster in this work.

## Example: what a stepdown looks like

The integration test `tests/integration/test_failover.py` runs 400 ops/s against a three-member replica set, marks "stepdown started", steps down the primary, and marks "stepdown finished". With retryable writes and reads turned off, the report shows a handful of `NotWritablePrimary` or `PrimarySteppedDown` failures, the primary change with its timestamp, and a recovery time. With retryable writes on, the same election usually shows no failures, only a latency spike and a write gap; that difference is the point of measuring.

The test `test_write_stall_measures_gap_latency_and_recovery` blocks writes on the primary for 3 seconds with `fsyncLock`. The report shows a write gap of about 3 seconds, a maximum response time of about 3 seconds, and pool checkout waits, because blocked writes held every pooled connection and reads queued behind them. That is the behavior an application with the same pool size would see.
