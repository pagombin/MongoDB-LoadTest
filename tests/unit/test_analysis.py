from ballast.engine.analysis import (
    AnalysisSettings,
    analyze_window,
    client_bound,
    detect_disruptions,
    full_analysis,
    marker_phase,
    marker_windows,
    summarize,
)
from ballast.engine.series import OpSecond, SecondRecord


def second(t, ok=1000, rt_us=2000, err=0, err_class="NotWritablePrimary", phase="steady", target=1000.0, cpu=20.0, first=None, last=None):
    op = OpSecond(ok=ok, err=err)
    op.rt.record(rt_us, ok + err)
    op.st.record(rt_us, ok + err)
    rec = SecondRecord(t=t, phase=phase, target_rate=target, ops={"update": op}, cpu=cpu)
    if err:
        rec.errors = {err_class: err}
        rec.first_err_ms = first if first is not None else t * 1000 + 100
        rec.last_err_ms = last if last is not None else t * 1000 + 900
    return rec


def scenario():
    records = []
    for t in range(0, 300):
        if t < 30:
            records.append(second(t, phase="warmup", rt_us=9000))
        elif 102 <= t <= 108:
            records.append(second(t, ok=200, err=50, rt_us=500_000, first=102_100 if t == 102 else None, last=108_800 if t == 108 else None))
        elif 110 <= t <= 115:
            records.append(second(t, rt_us=50_000))
        else:
            records.append(second(t))
    markers = [
        {"id": 1, "t_ms": 100_000, "label": "resize started"},
        {"id": 2, "t_ms": 110_000, "label": "resize finished"},
    ]
    gaps = [
        {"kind": "write", "start_ms": 102_300, "end_ms": 108_900},
        {"kind": "write", "start_ms": 200_000, "end_ms": 200_150},
        {"kind": "read", "start_ms": 104_000, "end_ms": 104_400},
    ]
    events = [{"t_ms": 103_000, "kind": "primary_changed", "data": {"from": "rs1:27011", "to": "rs2:27012"}}]
    return records, markers, gaps, events


def test_marker_phases_inferred_from_labels():
    assert marker_phase({"label": "resize started"}) == "begin"
    assert marker_phase({"label": "Resize finished"}) == "end"
    assert marker_phase({"label": "add member done"}) == "end"
    assert marker_phase({"label": "note", "phase": "begin"}) == "begin"
    assert marker_phase({"label": "checkpoint"}) is None


def test_marker_pairs_become_windows():
    _, markers, _, _ = scenario()
    windows = marker_windows(markers + [{"id": 3, "t_ms": 150_000, "label": "checkpoint"}])
    assert len(windows) == 1
    assert windows[0]["label"] == "resize"
    assert (windows[0]["start_ms"], windows[0]["end_ms"]) == (100_000, 110_000)


def test_impact_analysis_known_answers():
    records, markers, gaps, events = scenario()
    settings = AnalysisSettings()
    window = marker_windows(markers)[0]
    result = analyze_window(window, records, gaps, events, settings)
    assert result["first_failure_ms"] == 102_100
    assert result["last_failure_ms"] == 108_800
    assert result["failed_ops"] == 7 * 50
    assert result["failed_by_class"] == {"NotWritablePrimary": 350}
    assert result["longest_write_gap"] == {"ms": 6_600, "start_ms": 102_300, "end_ms": 108_900}
    assert result["longest_read_gap"]["ms"] == 400
    assert result["primary_changes"] == [{"t_ms": 103_000, "kind": "primary_changed", "from": "rs1:27011", "to": "rs2:27012"}]
    assert result["before"]["p99_us"] == 2_000
    assert result["before"]["throughput"] == 1_000
    assert result["during"]["max_us"] >= 500_000
    assert result["recovery_seconds"] == 6
    assert result["recovered"] is True
    assert result["after"]["p99_us"] == 50_000 * (1 + 0) or result["after"]["p99_us"] >= 49_000


def test_recovery_not_reached_reports_none():
    records = [second(t) for t in range(0, 100)] + [second(t, rt_us=80_000) for t in range(100, 160)]
    window = {"label": "w", "source": "markers", "start_ms": 100_000, "end_ms": 101_000}
    result = analyze_window(window, records, [], [], AnalysisSettings())
    assert result["recovery_seconds"] is None
    assert result["recovered"] is False


def test_warmup_excluded_from_summary_and_baseline():
    records, _, _, _ = scenario()
    summary = summarize(records)
    assert summary["measured_seconds"] == 270
    assert summary["response_time"]["min_us"] == 2_000


def test_disruptions_detected_and_merged():
    records, _, _, _ = scenario()
    found = detect_disruptions(records, AnalysisSettings())
    assert len(found) == 1
    assert found[0]["start_ms"] == 102_000
    assert found[0]["end_ms"] == 116_000


def test_client_bound_rate_shortfall():
    records = [second(t, ok=1_000, target=2_000.0) for t in range(0, 60)]
    result = client_bound(records, AnalysisSettings())
    assert result["client_bound"] is True
    assert result["reasons"][0]["kind"] == "rate"


def test_client_bound_cpu_and_short_bursts_ignored():
    records = [second(t, cpu=99.0 if 10 <= t < 25 else 30.0) for t in range(0, 60)]
    records += [second(t, cpu=99.0 if t < 65 else 30.0) for t in range(60, 80)]
    result = client_bound(records, AnalysisSettings())
    kinds = [(r["kind"], r["start"], r["end"]) for r in result["reasons"]]
    assert kinds == [("cpu", 10, 24)]


def test_rate_shortfall_during_disruption_is_not_client_bound():
    records, _, _, _ = scenario()
    settings = AnalysisSettings()
    result = client_bound(records, settings, detect_disruptions(records, settings))
    assert result["client_bound"] is False


def test_full_analysis_contains_marker_and_detected_windows():
    records, markers, gaps, events = scenario()
    result = full_analysis(records, gaps, events, markers, AnalysisSettings())
    sources = [i["source"] for i in result["impacts"]]
    assert sources == ["markers", "detected"]
    assert result["impacts"][1]["overlaps_marker_window"] is True
