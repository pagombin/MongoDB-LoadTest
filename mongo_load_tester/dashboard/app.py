"""Dash app for live MongoDB load-test telemetry."""

from __future__ import annotations

from datetime import UTC, datetime

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, callback, dcc, html

from mongo_load_tester.config import AppConfig
from mongo_load_tester.dashboard.export import build_csv_bytes, build_pdf_report, build_png_bytes
from mongo_load_tester.dashboard.figures import (
    build_latency_timeline,
    build_operation_distribution,
    build_operations_timeline,
)
from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.runner import LoadTestRunner


def create_dashboard(config: AppConfig, metrics_store: MetricsStore, runner: LoadTestRunner) -> Dash:
    app = Dash(__name__, title="MongoDB Load Tester")

    app.layout = html.Div(
        [
            html.H1("MongoDB Load Tester Dashboard"),
            html.Div(id="runner-status"),
            html.Div(
                [
                    _metric_card("Total Ops", "total-ops"),
                    _metric_card("Ops / sec", "ops-per-sec"),
                    _metric_card("Error Rate %", "error-rate"),
                    _metric_card("Avg Latency (ms)", "avg-latency"),
                    _metric_card("P95 Latency (ms)", "p95-latency"),
                    _metric_card("P99 Latency (ms)", "p99-latency"),
                ],
                style={"display": "grid", "gridTemplateColumns": "repeat(3, 1fr)", "gap": "10px"},
            ),
            html.Div(
                [
                    html.Button("Download CSV", id="download-csv-btn", n_clicks=0),
                    html.Button("Download PNG", id="download-png-btn", n_clicks=0),
                    html.Button("Download PDF", id="download-pdf-btn", n_clicks=0),
                ],
                style={"marginTop": "14px", "display": "flex", "gap": "8px"},
            ),
            dcc.Download(id="download-csv"),
            dcc.Download(id="download-png"),
            dcc.Download(id="download-pdf"),
            dcc.Graph(id="ops-graph"),
            dcc.Graph(id="latency-graph"),
            dcc.Graph(id="distribution-graph"),
            dcc.Interval(id="refresh", interval=config.dashboard.refresh_interval_ms, n_intervals=0),
        ],
        style={"padding": "16px", "fontFamily": "Arial, sans-serif"},
    )

    @callback(
        Output("runner-status", "children"),
        Output("total-ops", "children"),
        Output("ops-per-sec", "children"),
        Output("error-rate", "children"),
        Output("avg-latency", "children"),
        Output("p95-latency", "children"),
        Output("p99-latency", "children"),
        Output("ops-graph", "figure"),
        Output("latency-graph", "figure"),
        Output("distribution-graph", "figure"),
        Input("refresh", "n_intervals"),
    )
    def refresh_dashboard(_: int):
        snapshot = metrics_store.snapshot()
        error_rate = (snapshot.error_ops / snapshot.total_ops * 100.0) if snapshot.total_ops else 0.0
        if runner.last_error:
            status = f"Runner status: failed ({runner.last_error})"
        elif runner.is_running:
            status = "Runner status: running"
        elif runner.finished_at:
            status = f"Runner status: completed at {runner.finished_at.isoformat()}"
        else:
            status = "Runner status: initializing"
        return (
            status,
            _fmt(snapshot.total_ops),
            f"{snapshot.ops_per_second:.2f}",
            f"{error_rate:.2f}",
            f"{snapshot.avg_latency_ms:.2f}",
            f"{snapshot.p95_latency_ms:.2f}",
            f"{snapshot.p99_latency_ms:.2f}",
            build_operations_timeline(snapshot),
            build_latency_timeline(snapshot),
            build_operation_distribution(snapshot),
        )

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
            html.Div(id=value_id, style={"fontSize": "24px", "fontWeight": "600"}),
        ],
        style={"border": "1px solid #d1d5db", "padding": "12px", "borderRadius": "8px"},
    )


def _fmt(value: int) -> str:
    return f"{value:,}"
