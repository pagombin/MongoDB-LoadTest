# How Ballast measures

Numbers from Ballast are meant to be used for decisions. This page says exactly how each one is produced and what it does not cover.

## Clocks

- Durations use the monotonic clock (`time.monotonic()`), which never jumps when the system clock is adjusted.
- Each operation is assigned to the UTC wall-clock second in which it completed.
- Timestamps are stored in UTC, as milliseconds since the Unix epoch. The UI shows them in the browser's time zone, or in UTC if you choose that in **Settings**.

## Response time and service time

For every operation Ballast records two durations:

- **Service time**: from the moment the operation was handed to the driver until it completed. This is what a client library would usually report.
- **Response time**: from the moment the operation was **supposed** to start until it completed.

In the closed model the two are the same, because each worker starts its next operation as soon as the previous one finishes. In the open model they differ whenever the generator falls behind its schedule; see coordinated omission below.

Failed operations are included in both, measured to the moment the failure was returned. A timeout that took 30 seconds counts as a 30-second operation.

Application-level retries (the profile's `retry.max_attempts`, off by default) are inside the measured time: the operation's latency runs from its first attempt to its final result. Driver retries (retryable writes and reads) are always inside it too, because the driver does them before returning.

## Load models

**Open model** (default). You set a target rate *R*. Operation *i* is scheduled to start at `t0 + i / R`. A fixed pool of in-flight slots (`concurrency`) takes the next scheduled operation, waits until its start time if it is early, sends it, and records the result. If the database slows down, scheduled start times keep advancing regardless, so a backlog of late operations builds up and is sent as soon as slots free up. Only the index of the next operation is stored, so a long stall costs memory proportional to the number of slots, not the backlog.

Pausing shifts `t0` by the length of the pause. Changing the rate during a run starts the new rate at the oldest operation not yet sent, or now if none is late, so operations already late stay late and their latency still counts. Idle slots check every 100 ms whether the schedule changed, so a rate increase takes effect within 100 ms.

**Closed model**. You set a number of workers. Each sends one operation, waits for it, optionally sleeps the think time, and repeats. Throughput is whatever the database allows. This model cannot show how late requests would have been, because a slow database simply makes the workers send less.

## Coordinated omission

A generator that waits for each response before sending the next request "coordinates" with the database: during a stall it stops sending, so it records one slow operation instead of the hundreds that real users would have experienced. Percentiles computed that way look far better than reality.

Ballast avoids this in the open model by measuring response time from the scheduled start. If the database stalls for 3 seconds at 1,000 operations per second, about 3,000 operations record latencies between 0 and 3 seconds, and the p99 reflects it. The service time is reported next to it so you can see how much of the latency was spent waiting to be sent.

The test `tests/unit/test_open_model.py::test_stall_is_reported_as_latency_from_intended_start` runs the scheduling code against a service that stalls for 0.5 seconds and checks that the stall appears in the 90th percentile of response time but not of service time.

## Histograms and percentiles

Latencies are recorded in microseconds in a log-linear histogram with the same bucket layout as HdrHistogram configured for 3 significant digits:

- Values below 2,048 µs are recorded exactly.
- Above that, each bucket is at most 1/1024 of its value wide, so any reported value is within 0.1% of a value that was actually recorded.
- The range is 0 to 1 hour; longer values are clamped to 1 hour.

A percentile is reported as the highest value in the bucket that contains it (never more than the observed maximum), so reported percentiles are never lower than the true sample percentile.

Every second, for every operation type, Ballast stores one histogram of response time and one of service time. Percentiles over any longer period (the run summary, an impact window, a downsampled chart point, the comparison table) come from **merging** the per-second histograms (adding their counts) and reading the percentile from the merged histogram. Percentiles are never averaged. With several generator processes, their histograms for the same second are merged the same way.

`tests/unit/test_histogram.py` feeds 200,000 samples from uniform, exponential, lognormal and bimodal distributions through the histogram and checks every reported percentile against the exact sorted-sample percentile, within the stated precision. It also checks that merging ten histograms equals recording everything in one.

## What is excluded from summaries

The run summary, the operations table and impact baselines exclude seconds labelled:

- `load`: the data load phase, which is not measured.
- `warmup`: the warm-up period, shaded on charts.
- `paused`: seconds in which the run was paused, including the seconds it was paused and resumed in.
- `stopping`: the drain after a stop, including the partial final second.

Charts still show these seconds, shaded, so nothing is hidden.

## Throughput

Throughput is successful operations per second. Failed operations are shown separately. The "target" line is the open-model rate at that second. Charts with more points than fit are downsampled by merging whole seconds into buckets of 2, 5, 10, 15, 30 or 60 seconds or more; the label above the chart says so, and per-second values are always available in the CSV and JSON exports.

## Errors

Every failed operation is classified by the driver's error, not by its message text:

- Server errors use the code name (`NotWritablePrimary`, `PrimarySteppedDown`, `InterruptedDueToReplStateChange`, `WriteConflict`, ...). Known codes without a code name are named from a table; others are `Code<N>`.
- Driver-side failures are `NetworkError`, `NetworkTimeout`, `ServerSelectionTimeout`, `OperationTimeout` (client-side `timeoutMS` or `maxTimeMS`) and `WriteConcernTimeout`.
- Error labels such as `RetryableWriteError` are counted separately.

For each second Ballast stores the count per class and the exact millisecond of the first and last failure.

## Gaps

A **write gap** is a period with no successful write (insert, update, upsert, delete, find-and-modify); a **read gap** is the same for reads. Each generator process tracks the time of its last success of each kind and reports a gap when the next success comes after more than the larger of 100 ms and four average intervals between successes. With several generator processes, a gap counts only for the time when every process was in a gap at once.

## Impact analysis

For each window (a pair of begin and end markers, a range you select in the report, or a disruption detected automatically):

- **Analysis span**: from the window start to the window end plus the recovery time (or plus the baseline length, if the workload did not recover). Failures, gaps and primary changes are counted over this span.
- **Before**: up to `baseline_seconds` (default 60) of measured seconds immediately before the window, excluding other marker windows.
- **During**: the seconds of the window.
- **After**: up to `baseline_seconds` of measured seconds after the window.
- **Recovery time**: the number of seconds from the window end until the first point from which, for `sustain_seconds` (default 5) consecutive seconds, successful throughput is at least the baseline minus `throughput_tolerance` (default 10%) and p99 response time is at most the baseline plus `latency_tolerance` (default 25%). Both are computed from merged histograms over those seconds. If that never happens before the run ends, the report says "not within run".
- **Primary changes**: driver topology events (a new primary, no primary, the same primary again) from 5 seconds before the window to the end of the span. They come from a dedicated monitoring client with a 500 ms heartbeat, so elections are timestamped to within about half a second.

**Detected disruptions** are runs of measured seconds with any failed operation, successful throughput below half the median of the seconds at the same load level (same phase, target rate and workers, so ramp steps and level changes are not disruptions), or p99 above four times the run's median p99 (and above 10 ms). Seconds less than 3 seconds apart are merged.

All thresholds can be changed per profile in `analysis` (see the profile JSON).

## Guardrails and pass/fail criteria

A guardrail looks at each measured second as it is written (warm-up, data load, pauses and the stop drain do not count). A second breaches when its failed share is above `max_error_percent`, or its p99 response time (from that second's merged histogram) is above `max_p99_ms`. After `sustain_seconds` breaching seconds in a row, the run stops with reason `guardrail`; one good second resets the count.

Pass/fail criteria are evaluated once, on the run summary described above (merged histograms over all measured seconds). `min_throughput_ratio` divides the mean successful throughput by the open-model target rate; in the closed model it is reported as not evaluated. A criterion that cannot be evaluated does not fail the run.

## Client-bound runs

A load test only measures the database if the generator keeps up. Ballast flags a run as **client-bound** when, for at least 10 consecutive measured seconds:

- **rate**: operations attempted were more than 5% below the open-model target, outside detected disruptions and the 10 seconds after them (a database stall legitimately lowers achieved rate); or
- **cpu**: a generator process used at least 90% of one CPU core; or
- **pool**: p99 connection-pool checkout wait was at least 5 ms outside disruptions, which means `maxPoolSize` is too small for the concurrency.

The flag and its reasons appear on the live view and at the top of the report. Fixes: add generator processes (each is a separate Python process with its own CPU), raise `maxPoolSize` or `concurrency`, or run Ballast on a larger host closer to the cluster. One generator process typically sustains a few thousand operations per second, depending on document size and host CPU.

## Server sampling

If enabled in the profile, a separate client polls `hello`, `serverStatus` and `replSetGetStatus` every 5 seconds and stores member states, replication lag and connection counts with the run. Managed database users often lack the `clusterMonitor` role; the first `Unauthorized` reply is recorded once, shown in the UI, and that command is not tried again during the run. Sampling never affects the measured workload, which uses its own clients.

## What Ballast does not measure

- Network time between the generator and the cluster is part of every latency. Run Ballast close to the application you are modelling.
- Ballast does not see server-side queueing separately from network time.
- Operations of a key that was deleted still count as successful reads that found nothing.
