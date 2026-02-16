"""Dash app for live MongoDB load-test telemetry."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, callback, dcc, html
from dash.exceptions import PreventUpdate

from mongo_load_tester.config import AppConfig
from mongo_load_tester.dashboard.export import build_csv_bytes, build_pdf_report, build_png_bytes
from mongo_load_tester.dashboard.figures import (
    build_latency_timeline,
    build_operation_distribution,
    build_operations_timeline,
)
from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.runner import LoadTestRunner
from mongo_load_tester.models import MetricsSnapshot
from mongo_load_tester.observability import InMemoryLogBuffer

CARD_STYLE = {"background": "white", "border": "1px solid #e5e7eb", "borderRadius": "10px", "padding": "14px"}
ACTION_BUTTON_STYLE = {
    "padding": "10px 12px",
    "borderRadius": "8px",
    "border": "1px solid #d1d5db",
    "background": "#f9fafb",
    "cursor": "pointer",
    "fontSize": "13px",
}
PRIMARY_BUTTON_STYLE = {
    "padding": "10px 12px",
    "borderRadius": "8px",
    "border": "1px solid #dc2626",
    "background": "#dc2626",
    "color": "white",
    "cursor": "pointer",
    "fontSize": "13px",
}
STATUS_STYLE_MAP = {
    "running": {"background": "#dcfce7", "color": "#166534", "border": "1px solid #86efac"},
    "completed": {"background": "#dbeafe", "color": "#1e40af", "border": "1px solid #93c5fd"},
    "failed": {"background": "#fee2e2", "color": "#991b1b", "border": "1px solid #fca5a5"},
    "initializing": {"background": "#fef9c3", "color": "#854d0e", "border": "1px solid #fde047"},
}


def create_dashboard(
    config: AppConfig,
    metrics_store: MetricsStore,
    runner: LoadTestRunner,
    log_buffer: InMemoryLogBuffer,
) -> Dash:
    app = Dash(__name__, title="MongoDB Load Tester")
    operation_mix = config.operation_mix.normalized()
    namespace_total = config.namespace.databases * config.namespace.collections_per_database
    read_ratio = operation_mix["read"] * 100
    write_ratio = 100 - read_ratio

    app.layout = html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.H1("MongoDB Load Tester", style={"margin": "0", "fontSize": "28px"}),
                            html.Div(
                                "Live OLTP-style load telemetry with one-click exports.",
                                style={"color": "#6b7280", "fontSize": "14px", "marginTop": "4px"},
                            ),
                        ]
                    ),
                    html.Div(
                        [
                            html.Button("Stop test", id="stop-test-btn", n_clicks=0, style=PRIMARY_BUTTON_STYLE),
                            html.Button("Export CSV", id="download-csv-btn", n_clicks=0, style=ACTION_BUTTON_STYLE),
                            html.Button("Export PNG", id="download-png-btn", n_clicks=0, style=ACTION_BUTTON_STYLE),
                            html.Button("Export PDF", id="download-pdf-btn", n_clicks=0, style=ACTION_BUTTON_STYLE),
                        ],
                        style={"display": "flex", "gap": "8px", "alignItems": "center"},
                    ),
                ],
                style={
                    "display": "flex",
                    "justifyContent": "space-between",
                    "alignItems": "center",
                    "gap": "14px",
                    **CARD_STYLE,
                },
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Run status", style={"fontSize": "12px", "color": "#6b7280"}),
                            html.Div(
                                id="runner-status",
                                style={
                                    "display": "inline-block",
                                    "marginTop": "8px",
                                    "padding": "6px 10px",
                                    "borderRadius": "999px",
                                    "fontWeight": "600",
                                    **STATUS_STYLE_MAP["initializing"],
                                },
                            ),
                            html.Div(
                                id="stop-feedback",
                                style={"marginTop": "8px", "fontSize": "12px", "color": "#6b7280"},
                            ),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            html.Div("Runtime", style={"fontSize": "12px", "color": "#6b7280"}),
                            html.Div(id="runtime-meta", style={"fontWeight": "600", "marginTop": "8px"}),
                            html.Progress(
                                id="test-progress",
                                value=0,
                                max=100,
                                style={"width": "100%", "height": "14px", "marginTop": "10px"},
                            ),
                            html.Div(
                                id="test-progress-label",
                                style={"marginTop": "6px", "fontSize": "12px", "color": "#6b7280"},
                            ),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            html.Div("Last refresh", style={"fontSize": "12px", "color": "#6b7280"}),
                            html.Div(id="last-updated", style={"fontWeight": "600", "marginTop": "8px"}),
                            html.Div(
                                "Adjust window",
                                style={"fontSize": "12px", "color": "#6b7280", "marginTop": "10px"},
                            ),
                            dcc.Dropdown(
                                id="timeline-window",
                                options=[
                                    {"label": "Last 1 minute", "value": 60},
                                    {"label": "Last 5 minutes", "value": 300},
                                    {"label": "Last 15 minutes", "value": 900},
                                    {"label": "All captured data", "value": 0},
                                ],
                                value=300,
                                clearable=False,
                                style={"marginTop": "6px"},
                            ),
                            html.Div(
                                id="window-description",
                                style={"marginTop": "6px", "fontSize": "12px", "color": "#6b7280"},
                            ),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "repeat(3, 1fr)", "gap": "12px"},
            ),
            html.Div(
                [
                    _profile_chip("Virtual users", f"{config.load.virtual_users:,}"),
                    _profile_chip("Worker threads", f"{config.load.worker_threads:,}"),
                    _profile_chip("Duration", _format_duration(config.load.duration_seconds)),
                    _profile_chip("Ramp-up", _format_duration(config.load.ramp_up_seconds)),
                    _profile_chip(
                        "Namespaces",
                        f"{config.namespace.databases} db x {config.namespace.collections_per_database} coll = {namespace_total}",
                    ),
                    _profile_chip("Read/Write mix", f"{read_ratio:.1f}% / {write_ratio:.1f}%"),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "repeat(6, 1fr)", "gap": "10px"},
            ),
            html.Div(
                [
                    _metric_card("Total ops", "total-ops"),
                    _metric_card("Ops/sec", "ops-per-sec"),
                    _metric_card("Error rate", "error-rate"),
                    _metric_card("Average latency", "avg-latency"),
                    _metric_card("P95 latency", "p95-latency"),
                    _metric_card("P99 latency", "p99-latency"),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "repeat(6, 1fr)", "gap": "10px"},
            ),
            html.Div(
                [
                    dcc.Graph(id="ops-graph", style={**CARD_STYLE}),
                    dcc.Graph(id="latency-graph", style={**CARD_STYLE}),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "2fr 2fr", "gap": "12px"},
            ),
            html.Div(
                [
                    dcc.Graph(id="distribution-graph", style={**CARD_STYLE}),
                    html.Div(
                        [
                            html.Div("Operation breakdown (overall)", style={"fontWeight": "600", "marginBottom": "8px"}),
                            html.Div(id="operation-table"),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1.5fr 2.5fr", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div("Live logs", style={"fontWeight": "600", "fontSize": "16px"}),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Div("Minimum level", style={"fontSize": "12px", "color": "#6b7280"}),
                                    dcc.Dropdown(
                                        id="log-level-filter",
                                        options=[
                                            {"label": "DEBUG", "value": "DEBUG"},
                                            {"label": "INFO", "value": "INFO"},
                                            {"label": "WARNING", "value": "WARNING"},
                                            {"label": "ERROR", "value": "ERROR"},
                                        ],
                                        value="INFO",
                                        clearable=False,
                                        style={"marginTop": "6px"},
                                    ),
                                ],
                                style={"minWidth": "170px"},
                            ),
                            html.Div(
                                [
                                    html.Div("Tail lines", style={"fontSize": "12px", "color": "#6b7280"}),
                                    dcc.Dropdown(
                                        id="log-line-count",
                                        options=[
                                            {"label": "100 lines", "value": 100},
                                            {"label": "250 lines", "value": 250},
                                            {"label": "500 lines", "value": 500},
                                            {"label": "1000 lines", "value": 1000},
                                        ],
                                        value=_default_log_tail(config.logging.ui_default_tail_lines),
                                        clearable=False,
                                        style={"marginTop": "6px"},
                                    ),
                                ],
                                style={"minWidth": "170px"},
                            ),
                            html.Div(
                                id="log-meta",
                                style={"fontSize": "12px", "color": "#6b7280", "alignSelf": "end", "paddingBottom": "6px"},
                            ),
                        ],
                        style={"display": "flex", "gap": "10px", "alignItems": "stretch", "marginTop": "8px"},
                    ),
                    html.Pre(
                        id="log-view",
                        style={
                            "marginTop": "10px",
                            "background": "#111827",
                            "color": "#e5e7eb",
                            "padding": "12px",
                            "borderRadius": "8px",
                            "fontSize": "12px",
                            "lineHeight": "1.45",
                            "maxHeight": "340px",
                            "overflowY": "auto",
                            "whiteSpace": "pre-wrap",
                        },
                    ),
                ],
                style={**CARD_STYLE, "marginTop": "12px"},
            ),
            dcc.Download(id="download-csv"),
            dcc.Download(id="download-png"),
            dcc.Download(id="download-pdf"),
            dcc.Interval(id="refresh", interval=config.dashboard.refresh_interval_ms, n_intervals=0),
        ],
        style={"padding": "16px", "fontFamily": "Inter, Arial, sans-serif", "background": "#f3f4f6"},
    )

    @callback(
        Output("runner-status", "children"),
        Output("runner-status", "style"),
        Output("runtime-meta", "children"),
        Output("last-updated", "children"),
        Output("test-progress", "value"),
        Output("test-progress-label", "children"),
        Output("window-description", "children"),
        Output("total-ops", "children"),
        Output("ops-per-sec", "children"),
        Output("error-rate", "children"),
        Output("avg-latency", "children"),
        Output("p95-latency", "children"),
        Output("p99-latency", "children"),
        Output("ops-graph", "figure"),
        Output("latency-graph", "figure"),
        Output("distribution-graph", "figure"),
        Output("operation-table", "children"),
        Output("stop-test-btn", "disabled"),
        Output("log-view", "children"),
        Output("log-meta", "children"),
        Input("refresh", "n_intervals"),
        Input("timeline-window", "value"),
        Input("log-level-filter", "value"),
        Input("log-line-count", "value"),
    )
    def refresh_dashboard(_: int, timeline_window: int, log_level: str, log_line_count: int):
        snapshot = metrics_store.snapshot()
        window_seconds = int(timeline_window or 0)
        filtered_snapshot = _filter_snapshot_timeline(snapshot, window_seconds)
        error_rate = (snapshot.error_ops / snapshot.total_ops * 100.0) if snapshot.total_ops else 0.0
        status_text, status_style = _build_status_ui(runner)
        runtime_meta, progress_value, progress_label = _build_runtime_ui(runner, config.load.duration_seconds)
        last_updated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        stop_disabled = not runner.is_running
        min_level = _parse_log_level(log_level)
        line_limit = int(log_line_count or config.logging.ui_default_tail_lines)
        lines = log_buffer.tail(limit=line_limit, min_level=min_level)
        log_text = "\n".join(lines) if lines else "No log entries yet."
        log_meta = f"Showing {len(lines)} line(s), level >= {(log_level or 'INFO').upper()}."

        return (
            status_text,
            {
                "display": "inline-block",
                "marginTop": "8px",
                "padding": "6px 10px",
                "borderRadius": "999px",
                "fontWeight": "600",
                **status_style,
            },
            runtime_meta,
            last_updated,
            progress_value,
            progress_label,
            _window_message(window_seconds),
            _fmt(snapshot.total_ops),
            f"{snapshot.ops_per_second:.2f}",
            f"{error_rate:.2f}%",
            f"{snapshot.avg_latency_ms:.2f} ms",
            f"{snapshot.p95_latency_ms:.2f} ms",
            f"{snapshot.p99_latency_ms:.2f} ms",
            build_operations_timeline(filtered_snapshot),
            build_latency_timeline(filtered_snapshot),
            build_operation_distribution(snapshot),
            _build_operation_table(snapshot),
            stop_disabled,
            log_text,
            log_meta,
        )

    @callback(Output("stop-feedback", "children"), Input("stop-test-btn", "n_clicks"), prevent_initial_call=True)
    def stop_test(n_clicks: int):
        if not n_clicks:
            raise PreventUpdate
        runner.stop()
        return "Stop requested. The workers are finishing in-flight operations."

    @callback(
        Output("download-csv", "data"),
        Input("download-csv-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def download_csv(_: int):
        rows = metrics_store.export_timeline_rows()
        csv_bytes = build_csv_bytes(rows)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return dcc.send_bytes(lambda out: out.write(csv_bytes), filename=f"mongodb_metrics_{timestamp}.csv")

    @callback(
        Output("download-png", "data"),
        Input("download-png-btn", "n_clicks"),
        State("ops-graph", "figure"),
        prevent_initial_call=True,
    )
    def download_png(_: int, ops_figure_payload: dict):
        figure = go.Figure(ops_figure_payload)
        png_bytes = build_png_bytes(
            figure,
            width=config.export.default_png_width,
            height=config.export.default_png_height,
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return dcc.send_bytes(lambda out: out.write(png_bytes), filename=f"mongodb_timeline_{timestamp}.png")

    @callback(
        Output("download-pdf", "data"),
        Input("download-pdf-btn", "n_clicks"),
        State("ops-graph", "figure"),
        State("latency-graph", "figure"),
        prevent_initial_call=True,
    )
    def download_pdf(_: int, ops_figure_payload: dict, latency_figure_payload: dict):
        snapshot = metrics_store.snapshot()
        pdf_bytes = build_pdf_report(
            snapshot=snapshot,
            ops_figure=go.Figure(ops_figure_payload),
            latency_figure=go.Figure(latency_figure_payload),
            image_width=config.export.default_png_width,
            image_height=config.export.default_png_height,
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return dcc.send_bytes(lambda out: out.write(pdf_bytes), filename=f"mongodb_report_{timestamp}.pdf")

    return app


def _metric_card(label: str, value_id: str) -> html.Div:
    return html.Div(
        [
            html.Div(label, style={"fontSize": "12px", "color": "#6b7280"}),
            html.Div(id=value_id, style={"fontSize": "22px", "fontWeight": "700", "marginTop": "6px"}),
        ],
        style={**CARD_STYLE, "minHeight": "78px"},
    )


def _profile_chip(label: str, value: str) -> html.Div:
    return html.Div(
        [
            html.Div(label, style={"fontSize": "11px", "color": "#6b7280"}),
            html.Div(value, style={"fontSize": "14px", "fontWeight": "600", "marginTop": "6px"}),
        ],
        style={**CARD_STYLE},
    )


def _build_operation_table(snapshot: MetricsSnapshot) -> html.Table:
    operations = ("read", "insert", "update", "delete")
    total_ops = snapshot.total_ops
    rows: list[html.Tr] = []
    for op_name in operations:
        count = snapshot.operation_counts.get(op_name, 0)
        share = (count / total_ops * 100.0) if total_ops else 0.0
        avg_latency = snapshot.operation_latency_avg_ms.get(op_name, 0.0)
        rows.append(
            html.Tr(
                [
                    html.Td(op_name, style={"padding": "8px", "borderBottom": "1px solid #e5e7eb"}),
                    html.Td(f"{count:,}", style={"padding": "8px", "borderBottom": "1px solid #e5e7eb"}),
                    html.Td(f"{share:.2f}%", style={"padding": "8px", "borderBottom": "1px solid #e5e7eb"}),
                    html.Td(f"{avg_latency:.2f} ms", style={"padding": "8px", "borderBottom": "1px solid #e5e7eb"}),
                ]
            )
        )
    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Operation", style={"textAlign": "left", "padding": "8px"}),
                        html.Th("Count", style={"textAlign": "left", "padding": "8px"}),
                        html.Th("Share", style={"textAlign": "left", "padding": "8px"}),
                        html.Th("Avg latency", style={"textAlign": "left", "padding": "8px"}),
                    ]
                )
            ),
            html.Tbody(rows),
        ],
        style={"width": "100%", "borderCollapse": "collapse", "fontSize": "13px"},
    )


def _build_status_ui(runner: LoadTestRunner) -> tuple[str, dict[str, str]]:
    if runner.last_error:
        return f"FAILED: {runner.last_error}", STATUS_STYLE_MAP["failed"]
    if runner.is_running:
        return "RUNNING", STATUS_STYLE_MAP["running"]
    if runner.finished_at:
        return "COMPLETED", STATUS_STYLE_MAP["completed"]
    return "INITIALIZING", STATUS_STYLE_MAP["initializing"]


def _build_runtime_ui(runner: LoadTestRunner, duration_seconds: int) -> tuple[str, float, str]:
    started_at = runner.started_at
    finished_at = runner.finished_at
    if not started_at:
        return "Waiting for runner startup", 0.0, "0.0% complete"
    if finished_at:
        elapsed = max((finished_at - started_at).total_seconds(), 0.0)
        return f"Completed in {elapsed:.1f}s", 100.0, "100.0% complete"
    now = datetime.now(UTC)
    elapsed = max((now - started_at).total_seconds(), 0.0)
    progress = min((elapsed / max(duration_seconds, 1)) * 100.0, 100.0)
    remaining = max(duration_seconds - elapsed, 0.0)
    return f"Elapsed: {elapsed:.1f}s | Remaining: {remaining:.1f}s", progress, f"{progress:.1f}% complete"


def _filter_snapshot_timeline(snapshot: MetricsSnapshot, window_seconds: int) -> MetricsSnapshot:
    if window_seconds <= 0:
        return snapshot
    cutoff = snapshot.generated_at - timedelta(seconds=window_seconds)
    filtered = [point for point in snapshot.timeline if point.second >= cutoff]
    return replace(snapshot, timeline=filtered)


def _window_message(window_seconds: int) -> str:
    if window_seconds <= 0:
        return "Showing all captured points."
    return f"Showing last {_format_duration(window_seconds)} of timeline data."


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    remaining_seconds = seconds % 60
    if minutes < 60:
        if remaining_seconds == 0:
            return f"{minutes}m"
        return f"{minutes}m {remaining_seconds}s"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if remaining_minutes == 0:
        return f"{hours}h"
    return f"{hours}h {remaining_minutes}m"


def _fmt(value: int) -> str:
    return f"{value:,}"


def _parse_log_level(level_name: str | None) -> int:
    if not level_name:
        return 20
    normalized = level_name.upper()
    if normalized == "DEBUG":
        return 10
    if normalized == "INFO":
        return 20
    if normalized == "WARNING":
        return 30
    if normalized == "ERROR":
        return 40
    return 20


def _default_log_tail(value: int) -> int:
    supported = [100, 250, 500, 1000]
    if value in supported:
        return value
    nearest = min(supported, key=lambda item: abs(item - value))
    return nearest
