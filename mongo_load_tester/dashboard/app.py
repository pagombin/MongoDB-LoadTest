"""Dash app for live MongoDB load-test telemetry."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, callback, ctx, dcc, html
from dash.exceptions import PreventUpdate
from plotly.subplots import make_subplots

from mongo_load_tester.config import AppConfig
from mongo_load_tester.dashboard.export import build_csv_bytes, build_pdf_report, build_png_bytes
from mongo_load_tester.dashboard.figures import (
    build_latency_timeline,
    build_operation_distribution,
    build_operations_timeline,
    build_user_ramp_timeline,
)
from mongo_load_tester.engine.control import LoadTestController
from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.runner import LoadTestRunner
from mongo_load_tester.models import MetricsSnapshot
from mongo_load_tester.observability import InMemoryLogBuffer

CARD_STYLE = {"background": "white", "border": "1px solid #d6d3f9", "borderRadius": "12px", "padding": "14px"}
ACTION_BUTTON_STYLE = {
    "padding": "10px 12px",
    "borderRadius": "8px",
    "border": "1px solid #c7d2fe",
    "background": "#eef2ff",
    "cursor": "pointer",
    "fontSize": "13px",
}
PAUSE_BUTTON_STYLE = {
    "padding": "10px 12px",
    "borderRadius": "8px",
    "border": "1px solid #f59e0b",
    "background": "#f59e0b",
    "color": "white",
    "cursor": "pointer",
    "fontSize": "13px",
}
RESUME_BUTTON_STYLE = {
    "padding": "10px 12px",
    "borderRadius": "8px",
    "border": "1px solid #10b981",
    "background": "#10b981",
    "color": "white",
    "cursor": "pointer",
    "fontSize": "13px",
}
STOP_BUTTON_STYLE = {
    "padding": "10px 12px",
    "borderRadius": "8px",
    "border": "1px solid #dc2626",
    "background": "#dc2626",
    "color": "white",
    "cursor": "pointer",
    "fontSize": "13px",
}
RESTART_BUTTON_STYLE = {
    "padding": "10px 12px",
    "borderRadius": "8px",
    "border": "1px solid #2563eb",
    "background": "#2563eb",
    "color": "white",
    "cursor": "pointer",
    "fontSize": "13px",
}
STATUS_STYLE_MAP = {
    "running": {"background": "#dcfce7", "color": "#166534", "border": "1px solid #86efac"},
    "paused": {"background": "#fef3c7", "color": "#92400e", "border": "1px solid #fcd34d"},
    "completed": {"background": "#dbeafe", "color": "#1e40af", "border": "1px solid #93c5fd"},
    "failed": {"background": "#fee2e2", "color": "#991b1b", "border": "1px solid #fca5a5"},
    "initializing": {"background": "#ede9fe", "color": "#5b21b6", "border": "1px solid #c4b5fd"},
}
KPI_THEMES = [
    ("#eff6ff", "#bfdbfe"),
    ("#ecfdf5", "#a7f3d0"),
    ("#fffbeb", "#fde68a"),
    ("#fdf2f8", "#fbcfe8"),
    ("#f5f3ff", "#d8b4fe"),
    ("#ecfeff", "#a5f3fc"),
    ("#fef2f2", "#fecaca"),
    ("#eef2ff", "#c7d2fe"),
]


def create_dashboard(
    config: AppConfig,
    metrics_store: MetricsStore,
    runner: LoadTestRunner,
    log_buffer: InMemoryLogBuffer,
    controller: LoadTestController,
) -> Dash:
    app = Dash(__name__, title="MongoDB Load Tester")
    logger = logging.getLogger("mongo_load_tester.dashboard")
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
                            html.H1("MongoDB Load Tester", style={"margin": "0", "fontSize": "30px", "color": "#312e81"}),
                            html.Div(
                                "Live OLTP telemetry, resilient control plane, and export-ready reports.",
                                style={"color": "#4c1d95", "fontSize": "14px", "marginTop": "4px"},
                            ),
                        ]
                    ),
                    html.Div(
                        [
                            html.Button("Pause", id="pause-test-btn", n_clicks=0, style=PAUSE_BUTTON_STYLE),
                            html.Button("Resume", id="resume-test-btn", n_clicks=0, style=RESUME_BUTTON_STYLE),
                            html.Button("Restart", id="restart-test-btn", n_clicks=0, style=RESTART_BUTTON_STYLE),
                            html.Button("Stop", id="stop-test-btn", n_clicks=0, style=STOP_BUTTON_STYLE),
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
                    "background": "linear-gradient(90deg, #eef2ff 0%, #faf5ff 100%)",
                    "border": "1px solid #c4b5fd",
                    "borderRadius": "12px",
                    "padding": "16px",
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
                            html.Div(id="control-feedback", style={"marginTop": "8px", "fontSize": "12px", "color": "#6b7280"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            html.Div("Runtime", style={"fontSize": "12px", "color": "#6b7280"}),
                            html.Div(id="runtime-meta", style={"fontWeight": "600", "marginTop": "8px", "color": "#111827"}),
                            html.Progress(
                                id="test-progress",
                                value=0,
                                max=100,
                                style={"width": "100%", "height": "14px", "marginTop": "10px"},
                            ),
                            html.Div(id="test-progress-label", style={"marginTop": "6px", "fontSize": "12px", "color": "#6b7280"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            html.Div("Control window", style={"fontSize": "12px", "color": "#6b7280"}),
                            html.Div(id="last-updated", style={"fontWeight": "600", "marginTop": "8px"}),
                            html.Div(
                                [
                                    dcc.Input(
                                        id="duration-seconds-input",
                                        type="number",
                                        min=1,
                                        step=1,
                                        value=config.load.duration_seconds,
                                        style={
                                            "width": "120px",
                                            "padding": "8px",
                                            "borderRadius": "8px",
                                            "border": "1px solid #d1d5db",
                                        },
                                    ),
                                    html.Button("Apply duration", id="apply-duration-btn", n_clicks=0, style=ACTION_BUTTON_STYLE),
                                ],
                                style={"display": "flex", "gap": "8px", "marginTop": "8px"},
                            ),
                            html.Div(id="duration-config-meta", style={"marginTop": "6px", "fontSize": "12px", "color": "#6b7280"}),
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
                    _profile_chip("Configured duration", _format_duration(config.load.duration_seconds)),
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
                    _metric_card("Total ops", "total-ops", KPI_THEMES[0]),
                    _metric_card(
                        "Ops/sec",
                        "ops-per-sec",
                        KPI_THEMES[1],
                        tooltip="Computed from total ops divided by active run time (paused time excluded).",
                    ),
                    _metric_card("Error rate", "error-rate", KPI_THEMES[2]),
                    _metric_card("Success rate", "success-rate", KPI_THEMES[3]),
                    _metric_card("Average latency", "avg-latency", KPI_THEMES[4]),
                    _metric_card("P95 latency", "p95-latency", KPI_THEMES[5]),
                    _metric_card("Retry rate", "retry-rate", KPI_THEMES[6]),
                    _metric_card("Active users", "active-users-live", KPI_THEMES[7]),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "repeat(4, 1fr)", "gap": "10px"},
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(
                                "Timeline filter (applies to all metric charts)",
                                style={"fontSize": "12px", "fontWeight": "600", "color": "#4c1d95"},
                            ),
                            dcc.Dropdown(
                                id="timeline-window",
                                options=[
                                    {"label": "Last 1 minute", "value": 60},
                                    {"label": "Last 5 minutes", "value": 300},
                                    {"label": "Last 15 minutes", "value": 900},
                                    {"label": "Last 30 minutes", "value": 1800},
                                    {"label": "Last 1 hour", "value": 3600},
                                    {"label": "Last 8 hours", "value": 28_800},
                                    {"label": "Last 24 hours", "value": 86_400},
                                    {"label": "All captured data", "value": 0},
                                ],
                                value=300,
                                clearable=False,
                                style={"marginTop": "6px", "width": "260px"},
                            ),
                            html.Div(
                                id="window-description",
                                style={"marginTop": "6px", "fontSize": "12px", "color": "#6b7280"},
                            ),
                        ],
                        style={**CARD_STYLE, "background": "#f8fafc", "border": "1px solid #c7d2fe"},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1fr", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Operations Throughput", style={"fontWeight": "600", "marginBottom": "6px", "color": "#312e81"}),
                            dcc.Graph(id="ops-graph", style={"height": "360px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            html.Div("Users Ramp View", style={"fontWeight": "600", "marginBottom": "6px", "color": "#312e81"}),
                            dcc.Graph(id="users-graph", style={"height": "360px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Latency Health", style={"fontWeight": "600", "marginBottom": "6px", "color": "#312e81"}),
                            dcc.Graph(id="latency-graph", style={"height": "340px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            html.Div("Operation Mix", style={"fontWeight": "600", "marginBottom": "6px", "color": "#312e81"}),
                            dcc.Graph(id="distribution-graph", style={"height": "340px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Operation breakdown (overall)", style={"fontWeight": "600", "marginBottom": "8px"}),
                            html.Div(id="operation-table"),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            html.Div("Smart insights", style={"fontWeight": "600", "marginBottom": "8px"}),
                            html.Div(id="smart-insights"),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1.2fr 0.8fr", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Minimum log level", style={"fontSize": "12px", "color": "#6b7280"}),
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
                        style={"minWidth": "180px"},
                    ),
                    html.Div(
                        [
                            html.Div("Log tail size", style={"fontSize": "12px", "color": "#6b7280"}),
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
                    html.Div(id="log-meta", style={"fontSize": "12px", "color": "#6b7280", "alignSelf": "end", "paddingBottom": "6px"}),
                ],
                style={**CARD_STYLE, "marginTop": "12px", "display": "flex", "gap": "12px", "alignItems": "stretch"},
            ),
            html.Pre(
                id="log-view",
                style={
                    "marginTop": "12px",
                    "background": "#111827",
                    "color": "#e5e7eb",
                    "padding": "12px",
                    "borderRadius": "8px",
                    "fontSize": "12px",
                    "lineHeight": "1.45",
                    "maxHeight": "340px",
                    "overflowY": "auto",
                    "whiteSpace": "pre-wrap",
                    "border": "1px solid #1f2937",
                },
            ),
            dcc.Download(id="download-csv"),
            dcc.Download(id="download-png"),
            dcc.Download(id="download-pdf"),
            dcc.Interval(id="refresh", interval=config.dashboard.refresh_interval_ms, n_intervals=0),
        ],
        style={"padding": "16px", "fontFamily": "Inter, Arial, sans-serif", "background": "#eef2ff"},
    )

    @callback(
        Output("runner-status", "children"),
        Output("runner-status", "style"),
        Output("runtime-meta", "children"),
        Output("last-updated", "children"),
        Output("test-progress", "value"),
        Output("test-progress-label", "children"),
        Output("window-description", "children"),
        Output("duration-config-meta", "children"),
        Output("total-ops", "children"),
        Output("ops-per-sec", "children"),
        Output("error-rate", "children"),
        Output("success-rate", "children"),
        Output("avg-latency", "children"),
        Output("p95-latency", "children"),
        Output("retry-rate", "children"),
        Output("active-users-live", "children"),
        Output("ops-graph", "figure"),
        Output("latency-graph", "figure"),
        Output("users-graph", "figure"),
        Output("distribution-graph", "figure"),
        Output("operation-table", "children"),
        Output("smart-insights", "children"),
        Output("stop-test-btn", "disabled"),
        Output("pause-test-btn", "disabled"),
        Output("resume-test-btn", "disabled"),
        Output("log-view", "children"),
        Output("log-meta", "children"),
        Input("refresh", "n_intervals"),
        Input("timeline-window", "value"),
        Input("log-level-filter", "value"),
        Input("log-line-count", "value"),
    )
    def refresh_dashboard(_: int, timeline_window: int, log_level: str, log_line_count: int):
        try:
            snapshot = metrics_store.snapshot()
            window_seconds = int(timeline_window or 0)
            filtered_snapshot = _filter_snapshot_timeline(snapshot, window_seconds)
            filtered_snapshot = _remove_incomplete_live_second(filtered_snapshot, runner.is_running)
            stable_ops_per_sec = _compute_stable_ops_per_sec(snapshot.total_ops, runner.active_elapsed_seconds())
            error_rate = (snapshot.error_ops / snapshot.total_ops * 100.0) if snapshot.total_ops else 0.0
            success_rate = (snapshot.success_ops / snapshot.total_ops * 100.0) if snapshot.total_ops else 0.0
            retry_rate = (snapshot.retried_ops / snapshot.total_ops * 100.0) if snapshot.total_ops else 0.0
            status_text, status_style = _build_status_ui(runner)
            runtime_meta, progress_value, progress_label = _build_runtime_ui(runner)
            last_updated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            duration_meta = (
                f"Configured run duration: {_format_duration(runner.target_duration_seconds)} "
                f"({runner.target_duration_seconds} sec)"
            )
            min_level = _parse_log_level(log_level)
            line_limit = int(log_line_count or config.logging.ui_default_tail_lines)
            lines = log_buffer.tail(limit=line_limit, min_level=min_level)
            log_text = "\n".join(lines) if lines else "No log entries yet."
            log_meta = f"Showing {len(lines)} line(s), level >= {(log_level or 'INFO').upper()}."
            stop_disabled = not runner.is_running
            pause_disabled = (not runner.is_running) or runner.is_paused
            resume_disabled = (not runner.is_running) or (not runner.is_paused)

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
                _window_message(window_seconds, snapshot),
                duration_meta,
                _fmt(snapshot.total_ops),
                f"{stable_ops_per_sec:.2f}",
                f"{error_rate:.2f}%",
                f"{success_rate:.2f}%",
                f"{snapshot.avg_latency_ms:.2f} ms",
                f"{snapshot.p95_latency_ms:.2f} ms",
                f"{retry_rate:.2f}%",
                _fmt(snapshot.active_virtual_users_current),
                build_operations_timeline(filtered_snapshot),
                build_latency_timeline(filtered_snapshot),
                build_user_ramp_timeline(filtered_snapshot),
                build_operation_distribution(snapshot),
                _build_operation_table(snapshot),
                _build_smart_insights(snapshot),
                stop_disabled,
                pause_disabled,
                resume_disabled,
                log_text,
                log_meta,
            )
        except Exception as exc:  # pragma: no cover - UI safety
            logger.exception("Dashboard refresh callback failed.")
            return _refresh_error_payload(str(exc))

    @callback(
        Output("control-feedback", "children"),
        Input("stop-test-btn", "n_clicks"),
        Input("pause-test-btn", "n_clicks"),
        Input("resume-test-btn", "n_clicks"),
        Input("restart-test-btn", "n_clicks"),
        Input("apply-duration-btn", "n_clicks"),
        State("duration-seconds-input", "value"),
        prevent_initial_call=True,
    )
    def control_runner(
        stop_clicks: int,
        pause_clicks: int,
        resume_clicks: int,
        restart_clicks: int,
        apply_clicks: int,
        duration_value: int,
    ):
        triggered = ctx.triggered_id
        if triggered is None:
            raise PreventUpdate
        try:
            if triggered == "stop-test-btn":
                if not stop_clicks:
                    raise PreventUpdate
                controller.stop()
                return "Stop requested. Workers are finishing in-flight operations."
            if triggered == "pause-test-btn":
                if not pause_clicks:
                    raise PreventUpdate
                paused = controller.pause()
                return "Load test paused." if paused else "Pause ignored (runner is not active)."
            if triggered == "resume-test-btn":
                if not resume_clicks:
                    raise PreventUpdate
                resumed = controller.resume()
                return "Load test resumed." if resumed else "Resume ignored (runner is not paused)."
            if triggered == "restart-test-btn":
                if not restart_clicks:
                    raise PreventUpdate
                _, message = controller.restart()
                return message
            if triggered == "apply-duration-btn":
                if not apply_clicks:
                    raise PreventUpdate
                if duration_value is None:
                    return "Duration value is required."
                updated = controller.update_duration_seconds(int(duration_value))
                return f"Run duration updated to {updated} seconds."
            raise PreventUpdate
        except Exception as exc:  # pragma: no cover - UI safety
            logger.exception("Runner control callback failed.")
            return f"Control action failed: {exc}"

    @callback(
        Output("download-csv", "data"),
        Input("download-csv-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def download_csv(_: int):
        try:
            rows = metrics_store.export_timeline_rows()
            csv_bytes = build_csv_bytes(rows)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            return dcc.send_bytes(lambda out: out.write(csv_bytes), filename=f"mongodb_metrics_{timestamp}.csv")
        except Exception:  # pragma: no cover - UI safety
            logger.exception("CSV export failed.")
            raise PreventUpdate

    @callback(
        Output("download-png", "data"),
        Input("download-png-btn", "n_clicks"),
        State("ops-graph", "figure"),
        State("users-graph", "figure"),
        prevent_initial_call=True,
    )
    def download_png(_: int, ops_figure_payload: dict, users_figure_payload: dict):
        try:
            combined = _build_combined_timeline_export_figure(
                ops_figure=go.Figure(ops_figure_payload),
                users_figure=go.Figure(users_figure_payload),
            )
            png_bytes = build_png_bytes(
                combined,
                width=config.export.default_png_width,
                height=config.export.default_png_height,
            )
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            return dcc.send_bytes(lambda out: out.write(png_bytes), filename=f"mongodb_timeline_{timestamp}.png")
        except Exception:  # pragma: no cover - UI safety
            logger.exception("PNG export failed.")
            raise PreventUpdate

    @callback(
        Output("download-pdf", "data"),
        Input("download-pdf-btn", "n_clicks"),
        State("ops-graph", "figure"),
        State("latency-graph", "figure"),
        State("users-graph", "figure"),
        prevent_initial_call=True,
    )
    def download_pdf(_: int, ops_figure_payload: dict, latency_figure_payload: dict, users_figure_payload: dict):
        try:
            snapshot = metrics_store.snapshot()
            pdf_bytes = build_pdf_report(
                snapshot=snapshot,
                ops_figure=go.Figure(ops_figure_payload),
                latency_figure=go.Figure(latency_figure_payload),
                users_figure=go.Figure(users_figure_payload),
                image_width=config.export.default_png_width,
                image_height=config.export.default_png_height,
            )
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            return dcc.send_bytes(lambda out: out.write(pdf_bytes), filename=f"mongodb_report_{timestamp}.pdf")
        except Exception:  # pragma: no cover - UI safety
            logger.exception("PDF export failed.")
            raise PreventUpdate

    return app


def _metric_card(label: str, value_id: str, theme: tuple[str, str], tooltip: str | None = None) -> html.Div:
    background, border = theme
    label_node: list[object] = [label]
    if tooltip:
        label_node.append(
            html.Span(
                "  i",
                title=tooltip,
                style={
                    "fontSize": "11px",
                    "padding": "2px 6px",
                    "marginLeft": "6px",
                    "border": "1px solid #a5b4fc",
                    "borderRadius": "999px",
                    "color": "#4338ca",
                    "cursor": "help",
                    "fontWeight": "600",
                },
            )
        )
    return html.Div(
        [
            html.Div(label_node, style={"fontSize": "12px", "color": "#4b5563"}),
            html.Div(id=value_id, style={"fontSize": "22px", "fontWeight": "700", "marginTop": "6px", "color": "#111827"}),
        ],
        style={
            "background": background,
            "border": f"1px solid {border}",
            "borderRadius": "10px",
            "padding": "12px",
            "minHeight": "78px",
        },
    )


def _profile_chip(label: str, value: str) -> html.Div:
    return html.Div(
        [
            html.Div(label, style={"fontSize": "11px", "color": "#6b7280"}),
            html.Div(value, style={"fontSize": "14px", "fontWeight": "600", "marginTop": "6px", "color": "#111827"}),
        ],
        style={**CARD_STYLE, "background": "#faf5ff", "border": "1px solid #d8b4fe"},
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
                    html.Td(op_name, style={"padding": "8px", "borderBottom": "1px solid #ddd6fe"}),
                    html.Td(f"{count:,}", style={"padding": "8px", "borderBottom": "1px solid #ddd6fe"}),
                    html.Td(f"{share:.2f}%", style={"padding": "8px", "borderBottom": "1px solid #ddd6fe"}),
                    html.Td(f"{avg_latency:.2f} ms", style={"padding": "8px", "borderBottom": "1px solid #ddd6fe"}),
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


def _build_smart_insights(snapshot: MetricsSnapshot) -> html.Ul:
    error_rate = (snapshot.error_ops / snapshot.total_ops * 100.0) if snapshot.total_ops else 0.0
    retry_rate = (snapshot.retried_ops / snapshot.total_ops * 100.0) if snapshot.total_ops else 0.0
    throughput_trend = _throughput_trend(snapshot)
    checks = [
        _insight_line("Error rate", error_rate, 1.0, lower_is_better=True, suffix="%"),
        _insight_line("P95 latency", snapshot.p95_latency_ms, 50.0, lower_is_better=True, suffix=" ms"),
        _insight_line("Retry rate", retry_rate, 2.0, lower_is_better=True, suffix="%"),
        _insight_line("Throughput trend", throughput_trend, 0.0, lower_is_better=False, suffix="%"),
    ]
    return html.Ul(checks, style={"margin": "0", "paddingLeft": "20px", "lineHeight": "1.8"})


def _insight_line(label: str, value: float, threshold: float, lower_is_better: bool, suffix: str) -> html.Li:
    good = value <= threshold if lower_is_better else value >= threshold
    color = "#166534" if good else "#b45309"
    state = "healthy" if good else "watch"
    return html.Li(
        [
            html.Span(f"{label}: ", style={"fontWeight": "600"}),
            html.Span(f"{value:.2f}{suffix}"),
            html.Span(
                f" ({state})",
                style={"marginLeft": "6px", "color": color, "fontWeight": "600"},
            ),
        ]
    )


def _throughput_trend(snapshot: MetricsSnapshot) -> float:
    points = snapshot.timeline
    if len(points) < 20:
        return 0.0
    midpoint = len(points) // 2
    first = points[:midpoint]
    second = points[midpoint:]
    first_avg = sum(point.total_ops for point in first) / max(len(first), 1)
    second_avg = sum(point.total_ops for point in second) / max(len(second), 1)
    if first_avg <= 0:
        return 0.0
    return ((second_avg - first_avg) / first_avg) * 100.0


def _build_status_ui(runner: LoadTestRunner) -> tuple[str, dict[str, str]]:
    if runner.last_error:
        return f"FAILED: {runner.last_error}", STATUS_STYLE_MAP["failed"]
    if runner.is_running and runner.is_paused:
        return "PAUSED", STATUS_STYLE_MAP["paused"]
    if runner.is_running:
        return "RUNNING", STATUS_STYLE_MAP["running"]
    if runner.finished_at:
        return "COMPLETED", STATUS_STYLE_MAP["completed"]
    return "INITIALIZING", STATUS_STYLE_MAP["initializing"]


def _build_runtime_ui(runner: LoadTestRunner) -> tuple[str, float, str]:
    started_at = runner.started_at
    finished_at = runner.finished_at
    target_duration = max(runner.target_duration_seconds, 1)
    active_elapsed = runner.active_elapsed_seconds()
    if not started_at:
        return "Waiting for runner startup", 0.0, "0.0% complete"
    if finished_at:
        elapsed = max((finished_at - started_at).total_seconds(), 0.0)
        return f"Completed in {elapsed:.1f}s", 100.0, "100.0% complete"
    progress = min((active_elapsed / target_duration) * 100.0, 100.0)
    remaining = max(target_duration - active_elapsed, 0.0)
    return f"Elapsed: {active_elapsed:.1f}s | Remaining: {remaining:.1f}s", progress, f"{progress:.1f}% complete"


def _filter_snapshot_timeline(snapshot: MetricsSnapshot, window_seconds: int) -> MetricsSnapshot:
    if window_seconds <= 0:
        return snapshot
    cutoff = snapshot.generated_at - timedelta(seconds=window_seconds)
    filtered = [point for point in snapshot.timeline if point.second >= cutoff]
    return replace(snapshot, timeline=filtered)


def _remove_incomplete_live_second(snapshot: MetricsSnapshot, is_running: bool) -> MetricsSnapshot:
    if not is_running or not snapshot.timeline:
        return snapshot
    current_second = snapshot.generated_at.replace(microsecond=0)
    filtered = [point for point in snapshot.timeline if point.second < current_second]
    return replace(snapshot, timeline=filtered)


def _window_message(window_seconds: int, snapshot: MetricsSnapshot) -> str:
    if window_seconds <= 0:
        return "Showing all captured points."
    if not snapshot.timeline:
        return f"Showing last {_format_duration(window_seconds)} (no data captured yet)."
    coverage_seconds = int((snapshot.timeline[-1].second - snapshot.timeline[0].second).total_seconds())
    message = f"Showing last {_format_duration(window_seconds)} of timeline data."
    if coverage_seconds > 0 and coverage_seconds < window_seconds:
        message += f" (currently {_format_duration(coverage_seconds)} available)"
    return message


def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
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
    return min(supported, key=lambda item: abs(item - value))


def _build_combined_timeline_export_figure(ops_figure: go.Figure, users_figure: go.Figure) -> go.Figure:
    combined = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.1,
        subplot_titles=("Operations Timeline", "Users Ramp Timeline"),
    )
    for trace in ops_figure.data:
        combined.add_trace(trace, row=1, col=1)
    for trace in users_figure.data:
        combined.add_trace(trace, row=2, col=1)
    combined.update_layout(
        template="plotly_white",
        height=900,
        margin={"l": 40, "r": 20, "t": 60, "b": 50},
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    combined.update_xaxes(title_text="Time (UTC)", row=2, col=1)
    combined.update_yaxes(title_text="Ops/sec", row=1, col=1)
    combined.update_yaxes(title_text="Active users", row=2, col=1)
    return combined


def _refresh_error_payload(error_message: str) -> tuple:
    style = {
        "display": "inline-block",
        "marginTop": "8px",
        "padding": "6px 10px",
        "borderRadius": "999px",
        "fontWeight": "600",
        **STATUS_STYLE_MAP["failed"],
    }
    empty_figure = go.Figure()
    empty_figure.update_layout(template="plotly_white")
    return (
        f"FAILED: dashboard refresh error ({error_message})",
        style,
        "Unavailable",
        datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        0.0,
        "0.0% complete",
        "Showing all captured points.",
        "Duration data unavailable",
        "0",
        "0.00",
        "0.00%",
        "0.00%",
        "0.00 ms",
        "0.00 ms",
        "0.00%",
        "0",
        empty_figure,
        empty_figure,
        empty_figure,
        empty_figure,
        html.Div("No operation data available due to dashboard refresh error."),
        html.Div("No insights available due to dashboard refresh error."),
        True,
        True,
        True,
        "Dashboard refresh failed. Check application logs for details.",
        "Showing 0 line(s), level >= INFO.",
    )


def _compute_stable_ops_per_sec(total_ops: int, active_elapsed_seconds: float) -> float:
    if total_ops <= 0:
        return 0.0
    if active_elapsed_seconds <= 0:
        return 0.0
    return total_ops / active_elapsed_seconds
