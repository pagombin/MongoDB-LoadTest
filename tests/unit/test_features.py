import http.server
import json
import threading

from aiohttp.test_utils import TestClient, TestServer

from ballast import cli, report
from ballast.config import load
from ballast.engine.analysis import GuardrailWatch, evaluate_slo
from ballast.engine.profile import Profile
from ballast.engine.series import OpSecond, SecondRecord
from ballast.notify import Notifier
from ballast.server import App, metrics_text
from ballast.store import Store

H = {"X-Ballast-Request": "1"}


def _second(t, ok=100, err=0, rt_us=2000, phase="steady"):
    op = OpSecond(ok=ok, err=err)
    op.rt.record(rt_us, ok + err)
    return SecondRecord(t=t, phase=phase, ops={"find": op})


def test_slo_evaluation():
    summary = {
        "response_time": {"p50_us": 1500, "p99_us": 12_000, "max_us": 90_000},
        "error_rate": 0.002,
        "throughput": 980,
        "measured_seconds": 60,
    }
    result = evaluate_slo(summary, {"p99_ms": 10, "error_percent": 0.5, "min_throughput_ratio": 0.95}, 1000)
    assert result["passed"] is False
    by_key = {c["key"]: c for c in result["criteria"]}
    assert by_key["p99_ms"]["passed"] is False and by_key["p99_ms"]["actual"] == 12
    assert by_key["error_percent"]["passed"] is True
    assert by_key["min_throughput_ratio"]["passed"] is True
    assert evaluate_slo(summary, {}, 1000) is None
    closed = evaluate_slo(summary, {"min_throughput_ratio": 0.9}, None)
    assert closed["passed"] is None and closed["criteria"][0]["actual"] is None


def test_guardrail_needs_consecutive_seconds_and_ignores_warmup():
    watch = GuardrailWatch(max_error_percent=5, max_p99_ms=None, sustain_seconds=3)
    assert watch.observe(_second(1, ok=50, err=50, phase="warmup")) is None
    assert watch.observe(_second(2, ok=50, err=50)) is None
    assert watch.observe(_second(3, ok=100)) is None
    assert watch.observe(_second(4, ok=90, err=10)) is None
    assert watch.observe(_second(5, ok=90, err=10)) is None
    trip = watch.observe(_second(6, ok=90, err=10))
    assert trip["t"] == 6 and "10.0% of operations failed" in trip["reasons"][0]
    assert watch.observe(_second(7, ok=0, err=100)) is None
    latency = GuardrailWatch(max_error_percent=None, max_p99_ms=100, sustain_seconds=1)
    assert latency.observe(_second(1, rt_us=250_000))["reasons"][0].startswith("p99 response time 250 ms")


def test_webhook_posts_slack_compatible_json():
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    notifier = Notifier(f"http://127.0.0.1:{server.server_port}/x", ["run_finished"], 5, "Ballast", "https://b.example/")
    notifier.notify("run_started", {"id": "r1", "name": "n"}, "ignored")
    notifier.notify("run_finished", {"id": "r1", "name": "n", "status": "completed"}, "run 'n' completed")
    thread.join(5)
    server.server_close()
    assert len(received) == 1
    assert received[0]["text"] == "Ballast: run 'n' completed <https://b.example/#/runs/r1|open report>"
    assert received[0]["run"]["url"] == "https://b.example/#/runs/r1"


def test_markdown_summary():
    profile = Profile.model_validate({"load": {"rate": 500}}).model_dump(mode="json")
    run = {
        "id": "r1",
        "name": "resize test",
        "status": "completed",
        "target_name": "prod",
        "database_name": "b",
        "started_at": 1_000_000,
        "ended_at": 1_060_000,
        "profile": profile,
        "tags": ["m40", "resize"],
        "notes": "M40 to M50",
    }
    analysis = {
        "summary": {
            "throughput": 499.5,
            "err": 12,
            "error_rate": 0.0004,
            "response_time": {"p50_us": 2100, "p99_us": 15_300, "p999_us": 210_000, "max_us": 4_100_000},
            "errors": {"NotWritablePrimary": 12},
        },
        "slo": evaluate_slo({"response_time": {"p99_us": 15_300}, "error_rate": 0.0004, "throughput": 499.5}, {"p99_ms": 20}, 500),
        "impacts": [
            {
                "label": "resize",
                "start_ms": 1_020_000,
                "failed_ops": 12,
                "longest_write_gap": {"ms": 4130},
                "longest_read_gap": None,
                "primary_changes": [{}],
                "before": {"p99_us": 6800},
                "during": {"p99_us": 3_980_000},
                "after": {"p99_us": 7000},
                "recovered": True,
                "recovery_seconds": 4,
            }
        ],
    }
    text = report.markdown_export(run, analysis)
    assert "## resize test" in text and "| 500 ops/s | 2.1 ms | 15.3 ms | 210 ms | 4,100 ms | 12 (0.04%) |" in text
    assert "**SLO: passed**" in text and "`m40`" in text and "M40 to M50" in text
    assert "| resize (00:17:00) | 12 | 4.13 s | 0.00 s | 1 | 6.8 ms → 3,980 ms → 7.0 ms | 4 s |" in text


def test_prometheus_text():
    state = {
        "id": "r1",
        "name": 'quote "x"',
        "status": "running",
        "phase": "steady",
        "last": {"ok": 998.0, "err": 2.0, "target": 1000.0, "p50": 1500, "p99": 9000, "max": 40000, "backlog": 0, "cpu": 31.0},
        "errors": {"NetworkTimeout": 5},
    }
    text = metrics_text({"version": "1.0.0"}, state, {"completed": 3})
    assert 'ballast_runs{status="completed"} 3' in text
    assert 'ballast_response_time_seconds{run="r1",name="quote \\"x\\"",quantile="0.99"} 0.009000' in text
    assert 'ballast_errors_total{run="r1",name="quote \\"x\\"",class="NetworkTimeout"} 5' in text
    assert metrics_text({"version": "1"}, None, {}).endswith("ballast_run_active 0\n")


async def test_run_meta_patch_metrics_and_markdown_export(tmp_path):
    loaded = load(None, ["tls.mode=off"], environ={}, home=tmp_path)
    app = App(loaded)
    client = TestClient(TestServer(app.build()))
    await client.start_server()
    try:
        target = app.store.save_target({"name": "t", "uri": "mongodb://h:1/", "tls": "off"})
        app.store.create_run("r1", "first", target, "db", Profile().model_dump(mode="json"))
        app.store.update_run("r1", status="completed", started_at=1000, ended_at=5000)
        r = await client.patch("/api/runs/r1", json={"name": "renamed", "notes": "n", "tags": ["b", "a", "a"]}, headers=H)
        assert r.status == 200
        body = await r.json()
        assert body["name"] == "renamed" and body["tags"] == ["a", "b"] and body["notes"] == "n"
        r = await client.patch("/api/runs/r1", json={"tags": ["bad tag!"]}, headers=H)
        assert r.status == 422
        r = await client.patch("/api/runs/r1", json={"name": "x"})
        assert r.status == 403
        listed = await (await client.get("/api/runs")).json()
        row = listed[0] if isinstance(listed, list) else listed["runs"][0]
        assert row["tags"] == ["a", "b"]
        r = await client.get("/api/runs/r1/export.md")
        assert r.status == 200 and "## renamed" in await r.text()
        r = await client.get("/metrics")
        assert r.status == 200 and 'ballast_runs{status="completed"} 1' in await r.text()
        r = await client.post("/api/runs/missing/rerun", json={}, headers=H)
        assert r.status == 404
        system = await (await client.get("/api/system")).json()
        assert any(c["name"] == "pymongo" for c in system["capabilities"])
    finally:
        await client.close()


def test_backup_and_restore(tmp_path, capsys):
    store = Store(tmp_path / "data" / "ballast.db")
    target = store.save_target({"name": "t", "uri": "mongodb://h:1/", "tls": "off"})
    store.create_run("r1", "keep me", target, "db", {})
    store.close()
    dest = tmp_path / "b.db"
    args = cli.build_parser().parse_args(["backup", str(dest)])
    import os

    os.environ["BALLAST_HOME"] = str(tmp_path)
    try:
        assert args.func(args) == 0
        assert oct(dest.stat().st_mode)[-3:] == "600"
        store = Store(tmp_path / "data" / "ballast.db")
        store.delete_run("r1")
        store.close()
        args = cli.build_parser().parse_args(["restore", str(dest)])
        assert args.func(args) == 0
        store = Store(tmp_path / "data" / "ballast.db")
        assert [r["name"] for r in store.list_runs()] == ["keep me"]
        store.close()
        assert list((tmp_path / "data" / "backups").glob("ballast-pre-restore-*.db"))
        args = cli.build_parser().parse_args(["restore", str(tmp_path / "nothing.db")])
        assert args.func(args) == 1
    finally:
        del os.environ["BALLAST_HOME"]
