"""Impact analysis of scale events, disruption detection, and client-bound detection.

All latency figures come from merged histograms over the seconds in question,
never from averaging per-second percentiles.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, asdict

from ballast.engine.histogram import Histogram
from ballast.engine.series import EXCLUDED_PHASES, SecondRecord

BEGIN_RE = re.compile(r"(?:^|\b)(start|started|starting|begin|began|beginning)\s*$", re.I)
END_RE = re.compile(r"(?:^|\b)(finish|finished|end|ended|done|complete|completed|stop|stopped)\s*$", re.I)


@dataclass
class AnalysisSettings:
    baseline_seconds: int = 60
    sustain_seconds: int = 5
    throughput_tolerance: float = 0.10
    latency_tolerance: float = 0.25
    disruption_throughput_drop: float = 0.5
    disruption_latency_factor: float = 4.0
    disruption_latency_floor_us: int = 10_000
    disruption_merge_seconds: int = 3
    client_bound_rate_margin: float = 0.05
    client_bound_cpu_percent: float = 90.0
    client_bound_pool_wait_us: int = 5_000
    client_bound_min_seconds: int = 10

    @classmethod
    def from_dict(cls, data: dict | None) -> "AnalysisSettings":
        settings = cls()
        for key, value in (data or {}).items():
            if hasattr(settings, key) and value is not None:
                setattr(settings, key, type(getattr(settings, key))(value))
        return settings

    def as_dict(self) -> dict:
        return asdict(self)


def marker_phase(marker: dict) -> str | None:
    phase = marker.get("phase")
    if phase in ("begin", "end"):
        return phase
    label = (marker.get("label") or "").strip()
    if BEGIN_RE.search(label):
        return "begin"
    if END_RE.search(label):
        return "end"
    return None


def _base_label(label: str) -> str:
    stripped = BEGIN_RE.sub("", label)
    stripped = END_RE.sub("", stripped)
    return stripped.strip(" -_:") or label


def marker_windows(markers: list[dict]) -> list[dict]:
    """Pair each begin marker with the next end marker."""
    windows: list[dict] = []
    ordered = sorted(markers, key=lambda m: m["t_ms"])
    used: set = set()
    for i, marker in enumerate(ordered):
        if marker_phase(marker) != "begin":
            continue
        for later in ordered[i + 1:]:
            if later.get("id") in used:
                continue
            if marker_phase(later) == "end":
                used.add(later.get("id"))
                windows.append({
                    "label": _base_label(marker.get("label") or "window"),
                    "source": "markers",
                    "start_ms": marker["t_ms"],
                    "end_ms": later["t_ms"],
                    "begin_marker": marker.get("id"),
                    "end_marker": later.get("id"),
                })
                break
    return windows


def _measured(records: list[SecondRecord]) -> list[SecondRecord]:
    return [r for r in records if r.phase not in EXCLUDED_PHASES]


def _between(records: list[SecondRecord], start_s: int, end_s: int) -> list[SecondRecord]:
    return [r for r in records if start_s <= r.t < end_s]


def _stats(records: list[SecondRecord]) -> dict:
    hist = Histogram()
    ok = err = 0
    for record in records:
        hist.merge(record.rt())
        ok += record.ok
        err += record.err
    seconds = len(records)
    p = hist.percentiles((50, 99))
    return {
        "seconds": seconds,
        "ok": ok,
        "err": err,
        "throughput": round(ok / seconds, 2) if seconds else None,
        "p50_us": p[50] if hist.total else None,
        "p99_us": p[99] if hist.total else None,
        "max_us": hist.max if hist.total else None,
    }


def _gap_in_span(gaps: list[dict], kind: str, start_ms: int, end_ms: int) -> dict | None:
    best = None
    for gap in gaps:
        if gap["kind"] != kind:
            continue
        if gap["end_ms"] < start_ms or gap["start_ms"] > end_ms:
            continue
        length = gap["end_ms"] - gap["start_ms"]
        if best is None or length > best["ms"]:
            best = {"ms": length, "start_ms": gap["start_ms"], "end_ms": gap["end_ms"]}
    return best


def recovery_seconds(records: list[SecondRecord], end_s: int, baseline: dict, settings: AnalysisSettings, run_end_s: int) -> int | None:
    """Seconds from ``end_s`` until throughput and p99 stay within tolerance of the baseline."""
    base_tput = baseline.get("throughput")
    base_p99 = baseline.get("p99_us")
    if not base_tput:
        return None
    by_t = {r.t: r for r in records}
    sustain = max(1, settings.sustain_seconds)
    s = end_s
    while s + sustain <= run_end_s + 1:
        window = [by_t.get(t) for t in range(s, s + sustain)]
        if all(r is not None and r.phase != "paused" for r in window):
            stats = _stats(window)
            tput_ok = stats["throughput"] is not None and stats["throughput"] >= base_tput * (1 - settings.throughput_tolerance)
            lat_ok = not base_p99 or (stats["p99_us"] is not None and stats["p99_us"] <= base_p99 * (1 + settings.latency_tolerance))
            if tput_ok and lat_ok:
                return s - end_s
        s += 1
    return None


def analyze_window(
    window: dict,
    records: list[SecondRecord],
    gaps: list[dict],
    events: list[dict],
    settings: AnalysisSettings,
    exclude_windows: list[dict] | None = None,
) -> dict:
    start_ms, end_ms = int(window["start_ms"]), int(window["end_ms"])
    start_s, end_s = start_ms // 1000, max(start_ms // 1000 + 1, -(-end_ms // 1000))
    measured = _measured(records)
    others = [w for w in (exclude_windows or []) if w is not window]

    def outside_others(r: SecondRecord) -> bool:
        return not any(w["start_ms"] // 1000 <= r.t < -(-w["end_ms"] // 1000) for w in others)

    before = [r for r in _between(measured, start_s - settings.baseline_seconds, start_s) if outside_others(r)]
    during = _between(records, start_s, end_s)
    after = _between(measured, end_s, end_s + settings.baseline_seconds)
    baseline = _stats(before)
    run_end_s = records[-1].t if records else end_s
    recovery = recovery_seconds(measured, end_s, baseline, settings, run_end_s)
    span_end_ms = end_ms + 1000 * (recovery if recovery is not None else settings.baseline_seconds)
    span = _between(records, start_s, -(-span_end_ms // 1000))
    failures = {}
    first_fail = last_fail = None
    failed = 0
    for record in span:
        failed += record.err
        for cls, count in record.errors.items():
            failures[cls] = failures.get(cls, 0) + count
        if record.first_err_ms is not None and (first_fail is None or record.first_err_ms < first_fail):
            first_fail = record.first_err_ms
        if record.last_err_ms is not None and (last_fail is None or record.last_err_ms > last_fail):
            last_fail = record.last_err_ms
    primary_changes = [
        {"t_ms": e["t_ms"], "from": e["data"].get("from"), "to": e["data"].get("to")}
        for e in events
        if e["kind"] == "primary_changed" and start_ms - 5000 <= e["t_ms"] <= span_end_ms
    ]
    return {
        "label": window.get("label"),
        "source": window.get("source"),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "span_end_ms": span_end_ms,
        "first_failure_ms": first_fail,
        "last_failure_ms": last_fail,
        "failed_ops": failed,
        "failed_by_class": dict(sorted(failures.items(), key=lambda kv: -kv[1])),
        "longest_write_gap": _gap_in_span(gaps, "write", start_ms, span_end_ms),
        "longest_read_gap": _gap_in_span(gaps, "read", start_ms, span_end_ms),
        "primary_changes": primary_changes,
        "before": baseline,
        "during": _stats(during),
        "after": _stats(after),
        "recovery_seconds": recovery,
        "recovered": recovery is not None,
    }


def detect_disruptions(records: list[SecondRecord], settings: AnalysisSettings) -> list[dict]:
    measured = _measured(records)
    if len(measured) < 3:
        return []
    tputs = [r.ok for r in measured]
    p99s = [r.rt().percentile(99) for r in measured]
    median_tput = statistics.median(tputs)
    median_p99 = statistics.median([p for p in p99s if p] or [0])
    bad: list[int] = []
    for record, p99 in zip(measured, p99s):
        disturbed = record.err > 0
        if median_tput and record.ok < median_tput * (1 - settings.disruption_throughput_drop):
            disturbed = True
        if median_p99 and p99 > max(median_p99 * settings.disruption_latency_factor, settings.disruption_latency_floor_us):
            disturbed = True
        if disturbed:
            bad.append(record.t)
    windows: list[dict] = []
    for t in bad:
        if windows and t - windows[-1]["_last"] <= settings.disruption_merge_seconds:
            windows[-1]["_last"] = t
        else:
            windows.append({"_first": t, "_last": t})
    return [
        {"label": "Detected disruption", "source": "detected", "start_ms": w["_first"] * 1000, "end_ms": (w["_last"] + 1) * 1000}
        for w in windows
    ]


def _runs_of(flags: list[tuple[int, bool]], minimum: int) -> list[tuple[int, int]]:
    """Contiguous runs of flagged seconds lasting at least ``minimum`` seconds."""
    runs: list[tuple[int, int]] = []
    start = previous = None
    for t, flag in flags:
        if flag and start is not None and t == previous + 1:
            previous = t
            continue
        if start is not None and previous - start + 1 >= minimum:
            runs.append((start, previous))
        start = previous = t if flag else None
    if start is not None and previous - start + 1 >= minimum:
        runs.append((start, previous))
    return runs


CATCH_UP_SECONDS = 10


def client_bound(records: list[SecondRecord], settings: AnalysisSettings, disruptions: list[dict] | None = None) -> dict:
    """Decide whether the generator, not the database, limited the run."""
    measured = _measured(records)
    disturbed = set()
    for window in disruptions or []:
        disturbed.update(range(window["start_ms"] // 1000, -(-window["end_ms"] // 1000) + CATCH_UP_SECONDS))
    reasons: list[dict] = []
    rate_flags = []
    for r in measured:
        attempted = r.ok + r.err
        short = bool(r.target_rate) and attempted < r.target_rate * (1 - settings.client_bound_rate_margin) and r.t not in disturbed
        rate_flags.append((r.t, short))
    for start, end in _runs_of(rate_flags, settings.client_bound_min_seconds):
        sample = [r for r in measured if start <= r.t <= end]
        achieved = sum(r.ok + r.err for r in sample) / len(sample)
        target = sample[-1].target_rate or 0
        reasons.append({"kind": "rate", "start": start, "end": end,
                        "detail": f"Achieved {achieved:,.0f} of {target:,.0f} ops/s target"})
    cpu_flags = [(r.t, r.cpu >= settings.client_bound_cpu_percent) for r in measured]
    for start, end in _runs_of(cpu_flags, settings.client_bound_min_seconds):
        peak = max(r.cpu for r in measured if start <= r.t <= end)
        reasons.append({"kind": "cpu", "start": start, "end": end,
                        "detail": f"Generator CPU at {peak:.0f}% of one core"})
    pool_flags = [(r.t, r.pool_wait.percentile(99) >= settings.client_bound_pool_wait_us and r.t not in disturbed) for r in measured]
    for start, end in _runs_of(pool_flags, settings.client_bound_min_seconds):
        peak = max(r.pool_wait.percentile(99) for r in measured if start <= r.t <= end)
        reasons.append({"kind": "pool", "start": start, "end": end,
                        "detail": f"Connection pool checkout wait p99 reached {peak / 1000:.1f} ms; raise maxPoolSize"})
    return {"client_bound": bool(reasons), "reasons": reasons}


def summarize(records: list[SecondRecord]) -> dict:
    """Run summary over measured seconds (warm-up, data load and pauses excluded)."""
    measured = _measured(records)
    hist = Histogram()
    service = Histogram()
    per_op: dict[str, dict] = {}
    errors: dict[str, int] = {}
    labels: dict[str, int] = {}
    for record in measured:
        for name, op in record.ops.items():
            entry = per_op.setdefault(name, {"ok": 0, "err": 0, "rt": Histogram(), "st": Histogram()})
            entry["ok"] += op.ok
            entry["err"] += op.err
            entry["rt"].merge(op.rt)
            entry["st"].merge(op.st)
            hist.merge(op.rt)
            service.merge(op.st)
        for key, value in record.errors.items():
            errors[key] = errors.get(key, 0) + value
        for key, value in record.labels.items():
            labels[key] = labels.get(key, 0) + value
    seconds = len(measured)
    ok = sum(e["ok"] for e in per_op.values())
    err = sum(e["err"] for e in per_op.values())
    return {
        "measured_seconds": seconds,
        "ok": ok,
        "err": err,
        "error_rate": round(err / (ok + err), 6) if ok + err else 0.0,
        "throughput": round(ok / seconds, 2) if seconds else 0.0,
        "response_time": hist.summary(),
        "service_time": service.summary(),
        "operations": {
            name: {"ok": e["ok"], "err": e["err"], "response_time": e["rt"].summary(), "service_time": e["st"].summary()}
            for name, e in sorted(per_op.items())
        },
        "errors": dict(sorted(errors.items(), key=lambda kv: -kv[1])),
        "labels": dict(sorted(labels.items(), key=lambda kv: -kv[1])),
    }


def full_analysis(records: list[SecondRecord], gaps: list[dict], events: list[dict], markers: list[dict], settings: AnalysisSettings) -> dict:
    windows = marker_windows(markers)
    disruptions = detect_disruptions(records, settings)
    impacts = [analyze_window(w, records, gaps, events, settings, windows) for w in windows]
    for d in disruptions:
        covered = any(w["start_ms"] - 5000 <= d["start_ms"] <= w["end_ms"] + 60_000 for w in windows)
        result = analyze_window(d, records, gaps, events, settings, windows)
        result["overlaps_marker_window"] = covered
        impacts.append(result)
    return {
        "summary": summarize(records),
        "impacts": impacts,
        "client_bound": client_bound(records, settings, disruptions),
        "settings": settings.as_dict(),
    }
