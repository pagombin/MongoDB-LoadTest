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
    header = ["timestamp_utc", "epoch_s", "phase", "ok_ops", "failed_ops", "target_rate", "concurrency", "backlog",
              "rt_p50_ms", "rt_p90_ms", "rt_p99_ms", "rt_p999_ms", "rt_max_ms", "st_p50_ms", "st_p99_ms",
              "pool_wait_p99_ms", "pool_cleared", "checkout_failed", "generator_cpu_pct"]
    header += [f"ops_{op}" for op in ops] + [f"p99_ms_{op}" for op in ops] + [f"errors_{c}" for c in classes]
    writer.writerow(header)
    for r in records:
        rt = r.rt()
        st = r.st()
        p = rt.percentiles((50, 90, 99, 99.9))
        sp = st.percentiles((50, 99))
        row = [_iso(r.t), r.t, r.phase, r.ok, r.err, r.target_rate if r.target_rate is not None else "", r.concurrency, r.backlog,
               p[50] / 1000, p[90] / 1000, p[99] / 1000, p[99.9] / 1000, rt.max / 1000, sp[50] / 1000, sp[99] / 1000,
               r.pool_wait.percentile(99) / 1000, r.pool_cleared, r.checkout_failed, round(r.cpu, 1)]
        row += [(r.ops[op].ok + r.ops[op].err) if op in r.ops else 0 for op in ops]
        row += [r.ops[op].rt.percentile(99) / 1000 if op in r.ops else "" for op in ops]
        row += [r.errors.get(c, 0) for c in classes]
        writer.writerow(row)
    return out.getvalue()


def _meta(run: dict) -> dict:
    return {k: run.get(k) for k in ("id", "name", "status", "target_name", "database_name", "created_at", "started_at", "ended_at", "error")}


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
<header class="export-head"><span class="wordmark-text">{name}</span><span class="muted">Offline report, generated {_iso(payload['generated_at'] // 1000)}</span>
<button type="button" class="btn" id="print-btn">Print or save as PDF</button></header>
<main id="report" class="report-root" aria-live="polite"></main>
<script id="report-data" type="application/json">{data}</script>
<script>{scripts}
document.getElementById("print-btn").addEventListener("click", function () {{ window.print(); }});
BallastReport.render(document.getElementById("report"), JSON.parse(document.getElementById("report-data").textContent), {{ standalone: true }});
</script>
</body></html>"""
