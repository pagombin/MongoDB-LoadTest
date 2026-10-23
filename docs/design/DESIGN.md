# Ballast design plan

Audience: database and platform engineers watching a cluster while they change it. The product shows what the workload experienced. The live run view and the run report get the most care; every other screen exists to get a correct run started or to find an old one.

## 1. Principles

1. **Time is the spine.** Every chart on a run screen shares one time axis, one crosshair, and one zoom. Markers, topology events and error bursts sit on a lane under the charts at the same x positions, so cause and effect line up vertically.
2. **Numbers are text.** Measurements are set in the monospace face with tabular figures, carry their unit, and state what they are ("p99 response time, last 10 s"). A number without its window is not shown.
3. **Borders mean grouping, color means state.** No decorative shadows or gradients. A rule separates things that are read separately. Color is reserved for run state, error classes, and chart series; everything else is ink on paper.
4. **Controls that matter are always in reach.** On a live run, the marker button, pause, and stop are fixed in the run header and have keyboard shortcuts (`M`, `P`, `S`). Destructive actions name the thing they act on.
5. **Say what happened and what to do.** Empty states name the next action. Errors say what failed, the likely cause, and the fix. A button and its result use the same verb ("Stop run" then "Run stopped").

## 2. Color

Defined once as CSS custom properties in `ballast/web/css/tokens.css`. Both themes use the same token names.

### 2.1 Base palette

| Token | Role | Light | Dark |
|---|---|---|---|
| `--paper` | page background | `#F6F5F1` | `#0E1114` |
| `--surface` | panels, tables, inputs | `#FFFFFF` | `#161A1F` |
| `--ink` | primary text | `#15191E` | `#E7E5E0` |
| `--muted` | secondary text, axis labels | `#59606B` | `#9BA2AC` |
| `--rule` | borders and dividers | `#D8D5CC` | `#2B3139` |
| `--accent` | brand, primary action, focus ring | `#0B6E77` | `#43B3BD` |

State colors (used only for state):

| Token | Light | Dark | Use |
|---|---|---|---|
| `--ok` | `#2B7A4B` | `#5CC28A` | running, healthy, recovered |
| `--warn` | `#9A5800` | `#E0A145` | paused, client-bound, degraded |
| `--fail` | `#B3261E` | `#F07467` | failed, errors, interrupted |

Contrast checks (WCAG 2.2, normal text needs 4.5:1), computed with the WCAG relative luminance formula: `--ink` on `--paper` 16.2:1 light, 15.0:1 dark; `--muted` on `--paper` 5.8:1 light, 7.4:1 dark; `--accent` on `--surface` 6.0:1 light, 7.0:1 dark; `--fail` on `--surface` 6.5:1 light, 6.2:1 dark; `--warn` 5.6:1 and `--ok` 5.3:1 on white; white text on `--accent` 6.0:1. The accent color can be replaced by `branding.accent_color`; the server rejects colors that fall under 4.5:1 against white and logs a warning.

### 2.2 Chart palette

Based on the Okabe-Ito set, which stays distinguishable under the common forms of color blindness. Series are also separated by line style, so color is never the only cue.

| Token | Light | Dark | Series |
|---|---|---|---|
| `--c1` | `#0072B2` | `#56B4E9` | throughput, p50, run A |
| `--c2` | `#E69F00` | `#F0B840` | p99, run B |
| `--c3` | `#D55E00` | `#FF8A3D` | max (dashed), errors, run C |
| `--c4` | `#009E73` | `#3CC79E` | service time (dotted), run D |
| `--c5` | `#CC79A7` | `#E39BC4` | pool checkout wait, run E |
| `--c6` | `#6B6B6B` | `#A0A0A0` | target rate (dashed), baseline bands |

Warm-up is shaded with `--rule` at 40% opacity and labeled "warm-up" at the top of the band. Impact windows are shaded with `--accent` at 10%.

## 3. Type

- **IBM Plex Sans** (SIL OFL 1.1): all interface text.
- **IBM Plex Mono** (SIL OFL 1.1): measurements, IDs, timestamps, connection hosts, code.
- Self-hosted woff2 files in `ballast/web/fonts/`. No other font is requested.

| Step | Size / line height | Weight | Use |
|---|---|---|---|
| `--t-xs` | 12 / 16 | 400 | axis labels, table footers |
| `--t-sm` | 13 / 18 | 400, 500 | table cells, form help, lane labels |
| `--t-md` | 14 / 20 | 400, 500 | body, inputs, buttons |
| `--t-lg` | 16 / 22 | 600 | section headings |
| `--t-xl` | 20 / 26 | 600 | page titles |
| `--t-num` | 24 / 28 mono | 500 | key figures in a run header |

Compact density reduces row height from 36 px to 28 px and panel padding from 16 px to 10 px. Type sizes do not change.

Headings are sentence case. Labels are never all caps.

## 4. Layout

Desktop first, minimum supported width 1280 px, usable at 1024 px (tablet landscape): the left rail collapses to icons and run header figures wrap to two rows.

Shell: a 200 px left rail with the product mark and navigation, and a content area. The top of the content area is the page header. There is no global top bar.

### 4.1 Runs (home)

```
+-----------+----------------------------------------------------------------+
| BALLAST   | Runs                                          [ New run  N ]    |
|           |----------------------------------------------------------------|
| > Runs    | ACTIVE RUN BAND (only when a run is live)                      |
|   New run | ● Running  orders-steady-8k   12:04 elapsed   7,982 ops/s      |
|   Compare |   p99 4.1 ms   errors 0/s   [ Open live view ]                 |
|   Targets |----------------------------------------------------------------|
|   Profiles| Filter [ name / target ...    ] Status [All v]  Target [All v] |
|   ------- |----------------------------------------------------------------|
|   Settings| [ ] Name ^          Status     Target   Started     Dur  p99   |
|   System  | [ ] resize-m40     Completed  prod-rs  Oct 22 21:10 1:02 3.9ms |
|           | [ ] failover-test  Interrupt. dev-rs   Oct 22 18:00 0:14 22 ms |
|           | ...                                                            |
| v1.0.0    | 2 selected  [ Compare selected ]  [ Delete ]     page 1 of 3   |
+-----------+----------------------------------------------------------------+
```

Empty: "No runs yet. Add a target, then start a run." with buttons "Add target" and "New run".

### 4.2 New run (guided)

```
+-----------+----------------------------------------------------------------+
|           | New run                                                        |
|           | 1 Target -- 2 Workload -- 3 Schedule -- 4 Review               |
|           |----------------------------------------------------------------|
|           | Target                                                         |
|           | (o) prod-rs   mongodb+srv://bench@cluster0.x.net   [Test]      |
|           | ( ) dev-rs    mongodb://rs1:27011,rs2:27012                    |
|           |  Last test: 8.0.4, replica set rs0, primary rs1:27011          |
|           | Database for benchmark data [ ballast_bench ]                  |
|           |----------------------------------------------------------------|
|           |                                   [ Back ]  [ Next: workload ] |
+-----------+----------------------------------------------------------------+
```

Step 2 picks a saved profile or edits one inline (mix, documents, keys, driver settings, load model). Step 3 sets warm-up, ramp steps, steady duration or "until stopped", data load. Step 4:

```
| Review                                                                      |
| Ballast will connect to prod-rs (3 members, primary rs1) and write to       |
| database ballast_bench, collections bench_1 to bench_4.                     |
| It will first load 1,000,000 documents of about 1 KB (about 1 GB).          |
| Then it will send 8,000 operations per second (open model): 60% find by    |
| key, 30% update, 10% insert, keys zipfian.                                  |
| Warm-up 60 s (not counted), then steady until you stop it.                  |
|                                                                             |
| ! ballast_bench already contains 2 collections Ballast did not create:      |
|   users, sessions. Writes go only to bench_* collections, but they share    |
|   the database's cache and disk.                                            |
|   [x] I understand and want to continue                                     |
|                                                                             |
| Run name [ resize-m40-to-m50 ]                    [ Back ] [ Start run ]   |
```

### 4.3 Live run

```
+-----------+----------------------------------------------------------------+
|           | resize-m40-to-m50   ● Running  steady  14:02          [M] Mark |
|           | 7,982 ops/s  target 8,000 | p50 1.2 ms | p99 4.1 ms | err 0/s  |
|           |                                     [P] Pause   [S] Stop run   |
|           |----------------------------------------------------------------|
|           | Throughput (ops/s)                       window [5m|15m|1h|All]|
|           |  8k |~~~~~~~~~~~~~~~~~~~~\__/~~~~~~~~~~~~~~~~~~~~~~~~~~~       |
|           |     |   warm-up   :                                            |
|           | Response time (ms, log)                                        |
|           | 100 |                    /\                                    |
|           |   1 |~~~~~~~~~~~~~~~~~~~/  \~~~~~~~~~~~~~~~~~~~~~~~~~          |
|           |----------------------------------------------------------------|
|           | Markers    |        ▼resize started   ▼resize finished          |
|           | Topology   |             ◆ primary rs1→rs2                      |
|           | Errors     |             ▮▮▮                                    |
|           | Pool       |             ◇ cleared                              |
|           |----------------------------------------------------------------|
|           | Errors by class (live)    | Recent events                      |
|           | NotWritablePrimary 41     | 14:01:52 pool cleared rs1:27011    |
|           | NetworkTimeout      3     | 14:01:52 primary changed rs1→rs2   |
+-----------+----------------------------------------------------------------+
```

All three charts and the lane share x. Hovering shows one crosshair across all of them with a readout per series. Drag to zoom; double-click resets. The client-bound flag appears as a `--warn` chip next to the state ("Client-bound: achieved 6,100 of 8,000 ops/s, generator CPU 98%").

### 4.4 Run report

```
| resize-m40-to-m50   Completed   Oct 22 21:10-22:12 (1:02:11)                 |
| [ Export HTML ] [ CSV ] [ JSON ] [ Compare with... ]       Times: Local|UTC |
|------------------------------------------------------------------------------|
| Summary (excluding 60 s warm-up)                                             |
| ops   29.6M   ops/s 7,961   p50 1.2 ms   p99 4.0 ms   max 2.31 s   err 0.01% |
| Flags: none                                                                  |
|------------------------------------------------------------------------------|
| Charts (same as live view, full run, markers and events overlaid)           |
|------------------------------------------------------------------------------|
| Impact analysis                                                              |
| Window              First fail  Last fail  Failed  Write gap  Read gap  ...  |
| resize started→fin  21:31:04.2  21:31:11.9   1,204    7.6 s     0.4 s       |
|   p99 before/during/after: 4.0 / 1,830 / 4.2 ms   recovery 6 s   primary 1x |
| Detected 21:52:10   ...                                                      |
|------------------------------------------------------------------------------|
| Errors by class | Operations table | Configuration snapshot (collapsible)    |
```

### 4.5 Compare

```
| Compare   Runs: [resize-a x] [resize-b x] [+ add]   Align: (o) start ( ) marker [resize started v] |
| Throughput  (run A solid c1, run B dashed c2)                                |
| Response time p99                                                            |
| Table: run, ops/s, p50, p99, max, errors, write gap, recovery                |
```

### 4.6 Targets

```
| Targets                                                   [ Add target ]     |
| Name     Hosts                                   TLS   Last test            |
| prod-rs  cluster0.x.mongodb.net (SRV)            on    ok 8.0.4 rs0, 2m ago |
| dev-rs   rs1:27011, rs2:27012, rs3:27013         off   failed: auth failed  |
|------------------------------------------------------------------------------|
| Edit panel: name, connection string (password shown as •••• after save),    |
| TLS: auto/on/off, CA file path, allow invalid host names, [Test connection] |
| Test result: version, topology, members with state, primary, or the cause   |
| and suggested fix.                                                           |
```

### 4.7 Workload profiles

```
| Workload profiles                          [ Import ] [ New profile ]        |
| Name             Model           Mix                         Updated         |
| oltp-default     open 2,000/s    60 find / 30 upd / 10 ins   Oct 22          |
|   row actions: Edit  Duplicate  Export                                       |
|------------------------------------------------------------------------------|
| Editor sections: Operations | Documents | Keys and indexes | Driver | Load   |
```

### 4.8 Settings

```
| Settings                                                                     |
| Theme        (o) System ( ) Light ( ) Dark                                   |
| Density      (o) Comfortable ( ) Compact                                     |
| Times        (o) Local ( ) UTC                                               |
| Branding     product name, logo, accent (read from config, shown read-only  |
|              with the config key to change)                                  |
```

Theme, density and time zone are per browser (local storage). Branding is server configuration.

### 4.9 System

```
| System                                                 [ Download log bundle ]|
| Health    ok   uptime 3 d 2 h   listening https://127.0.0.1:8443             |
| Version   Ballast 1.0.0  commit 8a6e9e3  built 2026-10-23  driver 4.19.0     |
| Storage   data 412 MB (runs 37), logs 18 MB, free disk 41 GB                 |
| Certificate  self-signed, SHA-256 AB:CD:..., expires 2027-11-24              |
| Effective configuration (secrets redacted)                                   |
|   server.listen 127.0.0.1:8443  (file)                                       |
|   tls.mode self-signed          (default)                                    |
| Recent log lines (warning and above)                                         |
```

## 5. Components

One of each, used everywhere: button (primary, default, quiet, danger), text input, select, segmented control, checkbox, table with sortable headers, panel with heading rule, status chip, toast for confirmations, dialog for confirmations that need typing, inline error block with cause and fix. Focus ring: 2 px `--accent` outline with 2 px offset, never removed.

## 6. Motion

None by default except a 120 ms fade on toasts and dialogs. With `prefers-reduced-motion: reduce`, both are removed. Live charts update in place without easing.

## 7. Accessibility

- All controls are native elements with labels. Icons have text alternatives.
- Keyboard: Tab order follows reading order; shortcuts `N` new run, `M` mark, `P` pause/resume, `S` stop (asks to confirm), `/` focus filter, `?` lists shortcuts. Shortcuts are ignored while typing in a field.
- Each chart has a text summary and a "View as table" toggle.
- Live regions announce state changes ("Run paused") but not every data tick.
