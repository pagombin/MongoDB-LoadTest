"""Run exports: per-second CSV, full JSON, and a self-contained offline HTML report."""

from __future__ import annotations

import base64
import csv
import io
import json
import re
import time
from pathlib import Path

from ballast import product
from ballast.store import Store, intersect_gaps

WEB_DIR = Path(__file__).resolve().parent / "web"
REPORT_SCRIPTS = ("vendor/uplot.iife.min.js", "js/format.js", "js/charts.js", "js/report-view.js")
REPORT_STYLES = ("vendor/uplot.min.css", "css/tokens.css", "css/app.css")


def _iso(t: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def csv_export(store: Store, run: dict) -> str:
    records = store.seconds(run["id"])
    ops = sorted({op for r in records for op in r.ops})
    classes = sorted({c for r in records for c in r.errors})
    out = io.StringIO()
    writer = csv.writer(out)
    header = [
        "timestamp_utc",
        "epoch_s",
        "phase",
        "ok_ops",
        "failed_ops",
        "target_rate",
        "concurrency",
        "backlog",
        "rt_p50_ms",
        "rt_p90_ms",
        "rt_p99_ms",
        "rt_p999_ms",
        "rt_max_ms",
        "st_p50_ms",
        "st_p99_ms",
        "pool_wait_p99_ms",
        "pool_cleared",
        "checkout_failed",
        "generator_cpu_pct",
    ]
    header += [f"ops_{op}" for op in ops] + [f"p99_ms_{op}" for op in ops] + [f"errors_{c}" for c in classes]
    writer.writerow(header)
    for r in records:
        rt = r.rt()
        st = r.st()
        p = rt.percentiles((50, 90, 99, 99.9))
        sp = st.percentiles((50, 99))
        row = [
            _iso(r.t),
            r.t,
            r.phase,
            r.ok,
            r.err,
            r.target_rate if r.target_rate is not None else "",
            r.concurrency,
            r.backlog,
            p[50] / 1000,
            p[90] / 1000,
            p[99] / 1000,
            p[99.9] / 1000,
            rt.max / 1000,
            sp[50] / 1000,
            sp[99] / 1000,
            r.pool_wait.percentile(99) / 1000,
            r.pool_cleared,
            r.checkout_failed,
            round(r.cpu, 1),
        ]
        row += [(r.ops[op].ok + r.ops[op].err) if op in r.ops else 0 for op in ops]
        row += [r.ops[op].rt.percentile(99) / 1000 if op in r.ops else "" for op in ops]
        row += [r.errors.get(c, 0) for c in classes]
        writer.writerow(row)
    return out.getvalue()


def _meta(run: dict) -> dict:
    return {
        k: run.get(k) for k in ("id", "name", "status", "target_name", "database_name", "created_at", "started_at", "ended_at", "error")
    }


def json_export(store: Store, run: dict, analysis: dict | None) -> str:
    processes = (run.get("profile") or {}).get("load", {}).get("processes", 1)
    payload = {
        "format": f"{product.SLUG}-run",
        "version": 1,
        "exported_at": _iso(int(time.time())),
        "notes": "Latency values are microseconds. Histograms use the compact form [count, sum, min, max, index_deltas, counts]; see docs/METHODOLOGY.md.",
        "run": _meta(run),
        "profile": run.get("profile"),
        "snapshot": run.get("snapshot"),
        "analysis": analysis,
        "markers": store.markers(run["id"]),
        "events": store.events(run["id"]),
        "gaps": intersect_gaps(store.gaps(run["id"]), processes),
        "seconds": [r.to_json() for r in store.seconds(run["id"])],
    }
    return json.dumps(payload, default=str)


def _read(rel: str) -> str:
    return (WEB_DIR / rel).read_text(encoding="utf-8")


def _inline_fonts(css: str) -> str:
    def repl(match: re.Match) -> str:
        rel = match.group(1).lstrip("/")
        path = WEB_DIR / rel.replace("../", "")
        if not path.exists():
            return match.group(0)
        data = base64.b64encode(path.read_bytes()).decode()
        return f'url("data:font/woff2;base64,{data}")'

    return re.sub(r'url\("?((?:\.\./|/)?fonts/[^")]+\.woff2)"?\)', repl, css)


def report_payload(store: Store, run: dict, analysis: dict | None, about: dict, runs) -> dict:
    processes = (run.get("profile") or {}).get("load", {}).get("processes", 1)
    series = runs.series(run["id"], 3000)
    return {
        "run": {**_meta(run), "profile": run.get("profile"), "snapshot": run.get("snapshot")},
        "analysis": analysis,
        "series": series,
        "markers": store.markers(run["id"]),
        "events": store.events(run["id"]),
        "gaps": intersect_gaps(store.gaps(run["id"]), processes),
        "about": about,
        "generated_at": int(time.time() * 1000),
    }


def html_export(store: Store, run: dict, analysis: dict | None, about: dict, runs) -> str:
    payload = report_payload(store, run, analysis, about, runs)
    styles = "\n".join(_inline_fonts(_read(rel)) for rel in REPORT_STYLES)
    scripts = "\n".join(f"/* {rel} */\n" + _read(rel) for rel in REPORT_SCRIPTS)
    data = json.dumps(payload, default=str).replace("</", "<\\/")
    name = (about.get("product") or product.NAME).replace("<", "&lt;")
    title = f"{run['name']} report".replace("<", "&lt;")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{styles}</style>
</head>
<body class="report-export">
<header class="export-head"><span class="wordmark-text">{name}</span><span class="muted">Offline report, generated {_iso(payload["generated_at"] // 1000)}</span>
<button type="button" class="btn" id="print-btn">Print or save as PDF</button></header>
<main id="report" class="report-root" aria-live="polite"></main>
<script id="report-data" type="application/json">{data}</script>
<script>{scripts}
document.getElementById("print-btn").addEventListener("click", function () {{ window.print(); }});
BallastReport.render(document.getElementById("report"), JSON.parse(document.getElementById("report-data").textContent), {{ standalone: true }});
</script>
</body></html>"""


def _ms(us) -> str:
    if us is None:
        return "–"
    ms = us / 1000
    return f"{ms:,.0f} ms" if ms >= 100 else f"{ms:,.1f} ms" if ms >= 1 else f"{ms:,.2f} ms"


def _when(ms) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ms / 1000)) if ms else "–"


def markdown_export(run: dict, analysis: dict | None) -> str:
    """A short summary for tickets and chat: headline numbers, SLO verdict, and one line per impact window."""
    from ballast.engine.profile import Profile

    analysis = analysis or {}
    summary = analysis.get("summary") or run.get("summary") or {}
    rt = summary.get("response_time") or {}
    lines = [f"## {run['name']}", ""]
    try:
        workload = Profile.model_validate(run.get("profile") or {}).plain_summary()
    except Exception:
        workload = "–"
    duration = ""
    if run.get("started_at") and run.get("ended_at"):
        duration = f" ({(run['ended_at'] - run['started_at']) // 1000} s)"
    lines += [
        f"- **Status:** {run['status']}"
        + (f" ({run['stop_reason'].replace('_', ' ')})" if run.get("stop_reason") not in (None, "completed", "stopped") else ""),
        f"- **Target:** {run.get('target_name') or '–'}, database `{run.get('database_name') or '–'}`",
        f"- **When:** {_when(run.get('started_at') or run.get('created_at'))}{duration}",
        f"- **Workload:** {workload}",
    ]
    if run.get("tags"):
        lines.append("- **Tags:** " + ", ".join(f"`{t}`" for t in run["tags"]))
    lines += [
        "",
        "| Throughput | p50 | p99 | p99.9 | max | Failed |",
        "|---|---|---|---|---|---|",
        f"| {summary.get('throughput', 0):,.0f} ops/s | {_ms(rt.get('p50_us'))} | {_ms(rt.get('p99_us'))} | {_ms(rt.get('p999_us'))} | {_ms(rt.get('max_us'))} | "
        f"{summary.get('err', 0):,} ({summary.get('error_rate', 0) * 100:.2f}%) |",
    ]
    slo = analysis.get("slo")
    if slo:
        verdict = {True: "passed", False: "FAILED", None: "not evaluated"}[slo.get("passed")]
        lines += ["", f"**SLO: {verdict}**", ""]
        for c in slo["criteria"]:
            mark = {True: "pass", False: "FAIL", None: "n/a"}[c["passed"]]
            op = "≤" if c["kind"] == "max" else "≥"
            actual = "–" if c["actual"] is None else f"{c['actual']:,.4g}"
            lines.append(f"- {mark}: {c['label']} {actual}{(' ' + c['unit']) if c['unit'] else ''} ({op} {c['limit']:g})")
    bound = analysis.get("client_bound") or {}
    if bound.get("client_bound"):
        lines += ["", "> Client-bound: the load generator, not the database, limited this run. " + "; ".join(bound.get("reasons", []))]
    impacts = analysis.get("impacts") or []
    if impacts:
        lines += [
            "",
            "| Window | Failed | Write gap | Read gap | Primary changes | p99 before → during → after | Recovery |",
            "|---|---|---|---|---|---|---|",
        ]
        for imp in impacts:
            wg = imp.get("longest_write_gap") or {}
            rg = imp.get("longest_read_gap") or {}
            before, during, after = imp.get("before") or {}, imp.get("during") or {}, imp.get("after") or {}
            rec = f"{imp['recovery_seconds']} s" if imp.get("recovered") else "not within run"
            lines.append(
                f"| {imp.get('label') or 'window'} ({_when(imp['start_ms'])[11:19]}) | {imp.get('failed_ops', 0):,} | "
                f"{wg.get('ms', 0) / 1000:.2f} s | {rg.get('ms', 0) / 1000:.2f} s | {len(imp.get('primary_changes') or [])} | "
                f"{_ms(before.get('p99_us'))} → {_ms(during.get('p99_us'))} → {_ms(after.get('p99_us'))} | {rec} |"
            )
    errors = summary.get("errors") or {}
    if errors:
        lines += ["", "Failures by class: " + ", ".join(f"{cls} {n:,}" for cls, n in list(errors.items())[:8])]
    if run.get("notes"):
        lines += ["", run["notes"]]
    lines += [
        "",
        f"_{product.NAME} run `{run['id']}`. Latency is response time from the intended start; percentiles from merged histograms._",
        "",
    ]
    return "\n".join(lines)
