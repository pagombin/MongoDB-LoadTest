"""Dash app for live MongoDB load-test telemetry."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import plotly.graph_objects as go
import dash
from dash import Dash, Input, Output, State, callback, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from plotly.subplots import make_subplots

from mongo_load_tester.config import FAKER_PROVIDERS, FIELD_TYPES, AppConfig, SchemaConfig, default_config
from mongo_load_tester.dashboard.export import build_csv_bytes, build_pdf_report, build_png_bytes
from mongo_load_tester.dashboard.figures import (
    build_error_breakdown,
    build_error_retry_timeline,
    build_latency_histogram,
    build_latency_timeline,
    build_operation_distribution,
    build_operations_timeline,
    build_per_op_latency_timeline,
    build_per_op_throughput_timeline,
    build_user_ramp_timeline,
)
from mongo_load_tester.engine.control import LoadTestController
from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.runner import LoadTestRunner
from mongo_load_tester.metadata.store import MetadataStore
from mongo_load_tester.metadata.sync import ConfigSyncService
from mongo_load_tester.models import MetricsSnapshot
from mongo_load_tester.observability import InMemoryLogBuffer

CARD_STYLE = {
    "background": "white",
    "border": "1px solid #e5e7eb",
    "borderRadius": "14px",
    "padding": "18px",
    "boxShadow": "0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04)",
}
_BTN_BASE = {
    "padding": "9px 16px",
    "borderRadius": "8px",
    "cursor": "pointer",
    "fontSize": "13px",
    "fontWeight": "600",
    "letterSpacing": "0.01em",
    "transition": "all 0.15s ease",
}
ACTION_BUTTON_STYLE = {**_BTN_BASE, "border": "1px solid #c7d2fe", "background": "#eef2ff", "color": "#3730a3"}
PAUSE_BUTTON_STYLE = {**_BTN_BASE, "border": "none", "background": "#f59e0b", "color": "white"}
RESUME_BUTTON_STYLE = {**_BTN_BASE, "border": "none", "background": "#10b981", "color": "white"}
START_BUTTON_STYLE = {**_BTN_BASE, "border": "none", "background": "#059669", "color": "white"}
STOP_BUTTON_STYLE = {**_BTN_BASE, "border": "none", "background": "#dc2626", "color": "white"}
RESTART_BUTTON_STYLE = {**_BTN_BASE, "border": "none", "background": "#2563eb", "color": "white"}
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
_SECTION_HEADER_STYLE = {
    "fontSize": "16px",
    "fontWeight": "700",
    "color": "#1e1b4b",
    "marginTop": "28px",
    "marginBottom": "12px",
    "paddingBottom": "8px",
    "borderBottom": "2px solid #e0e7ff",
    "letterSpacing": "-0.01em",
}


def create_dashboard(
    config: AppConfig,
    metrics_store: MetricsStore,
    runner: LoadTestRunner,
    log_buffer: InMemoryLogBuffer,
    controller: LoadTestController,
    metadata_store: MetadataStore | None = None,
    config_sync_service: ConfigSyncService | None = None,
    initial_config_version: int = 1,
) -> Dash:
    app = Dash(__name__, title="MongoDB Load Tester")
    logger = logging.getLogger("mongo_load_tester.dashboard")
    class _ThreadSafeDict:
        """Thread-safe dict wrapper for shared dashboard state."""

        def __init__(self, initial: dict[str, object | None]):
            self._data = initial
            self._lock = threading.Lock()

        def get(self, key: str, default: object = None) -> object | None:
            with self._lock:
                return self._data.get(key, default)

        def __getitem__(self, key: str) -> object | None:
            with self._lock:
                return self._data[key]

        def __setitem__(self, key: str, value: object) -> None:
            with self._lock:
                self._data[key] = value

    run_state = _ThreadSafeDict({
        "active_run_id": None,
        "last_run_finished_at": None,
        "config_version": int(initial_config_version),
        "run_static_baseline": None,
        "startup_static_baseline": None,
        "staged_static_snapshot": None,
    })

    def persist_metadata_config(
        reason: str,
        config_override: AppConfig | None = None,
        mark_synced: bool = True,
    ) -> None:
        if metadata_store is None:
            return
        record = metadata_store.save_config(
            config_override or controller.current_config(),
            source="dashboard-ui",
            reason=reason,
        )
        run_state["config_version"] = record.version
        if mark_synced and config_sync_service is not None:
            config_sync_service.mark_applied_version(record.version)

    def apply_latest_metadata_config_before_start() -> None:
        if metadata_store is None:
            return
        record = metadata_store.fetch_config()
        if record is None:
            return
        controller.apply_config(record.config, source="start-preload")
        run_state["config_version"] = record.version
        if config_sync_service is not None:
            config_sync_service.mark_applied_version(record.version)

    def persist_run_completion_if_needed(snapshot: MetricsSnapshot) -> None:
        run_id = run_state.get("active_run_id")
        if not run_id:
            return
        if runner.is_running:
            return
        finished_at = runner.finished_at
        if finished_at is None:
            return
        if run_state.get("last_run_finished_at") == finished_at:
            return
        status = "failed" if runner.last_error else "completed"
        if metadata_store is not None:
            metadata_store.record_run_finished(run_id=str(run_id), status=status, snapshot=snapshot, error=runner.last_error)
        run_state["last_run_finished_at"] = finished_at
        run_state["active_run_id"] = None
        run_state["run_static_baseline"] = None
        run_state["staged_static_snapshot"] = None

    try:
        live_settings = controller.live_settings_snapshot()
        operation_mix = live_settings["operation_mix"]
    except Exception:  # pragma: no cover - startup safety
        logger.exception("Failed to load initial live settings. Falling back to static config values.")
        operation_mix = config.operation_mix.normalized()
    namespace_total = config.namespace.databases * config.namespace.collections_per_database
    read_ratio = operation_mix["read"] * 100
    write_ratio = 100 - read_ratio
    schema_fields_json_default = _schema_config_to_json(config.document_schema)
    run_state["startup_static_baseline"] = _static_config_snapshot(controller.current_config())

    app.index_string = app.index_string.replace(
        "</head>",
        "<style>"
        "@keyframes pulse {"
        "  0%, 100% { opacity: 1; transform: scale(1); }"
        "  50% { opacity: 0.85; transform: scale(1.005); box-shadow: 0 6px 20px rgba(239,68,68,0.25); }"
        "}"
        "</style></head>",
    )

    app.layout = html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.H1(
                                "MongoDB Load Tester",
                                style={
                                    "margin": "0",
                                    "fontSize": "28px",
                                    "fontWeight": "800",
                                    "color": "white",
                                    "letterSpacing": "-0.02em",
                                },
                            ),
                            html.Div(
                                "Live OLTP telemetry, resilient control plane, and export-ready reports.",
                                style={"color": "#c7d2fe", "fontSize": "13px", "marginTop": "2px"},
                            ),
                        ]
                    ),
                    html.Div(
                        [
                            html.Button(
                                "Start",
                                id="start-test-btn",
                                n_clicks=0,
                                style=START_BUTTON_STYLE,
                                title="Start load generation with current configuration.",
                            ),
                            html.Button(
                                "Pause",
                                id="pause-test-btn",
                                n_clicks=0,
                                style=PAUSE_BUTTON_STYLE,
                                title="Temporarily pause active load generation.",
                            ),
                            html.Button(
                                "Resume",
                                id="resume-test-btn",
                                n_clicks=0,
                                style=RESUME_BUTTON_STYLE,
                                title="Resume a paused load test.",
                            ),
                            html.Button(
                                "Restart",
                                id="restart-test-btn",
                                n_clicks=0,
                                style=RESTART_BUTTON_STYLE,
                                title="Restart immediately with current settings and cleared metrics.",
                            ),
                            html.Button(
                                "Stop",
                                id="stop-test-btn",
                                n_clicks=0,
                                style=STOP_BUTTON_STYLE,
                                title="Gracefully stop workers after current in-flight operations.",
                            ),
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
                    "background": "linear-gradient(135deg, #312e81 0%, #4c1d95 50%, #6d28d9 100%)",
                    "borderRadius": "16px",
                    "padding": "20px 24px",
                    "boxShadow": "0 4px 12px rgba(49,46,129,0.25)",
                },
            ),
            html.Div(
                id="scaling-alert-banner",
                style={"display": "none"},
            ),
            html.Details(
                [
                    html.Summary(
                        "Operator quick guide",
                        style={"cursor": "pointer", "fontWeight": "700", "color": "#312e81", "fontSize": "14px"},
                    ),
                    html.Div(
                        [
                            html.Div(
                                "Getting started",
                                style={"fontWeight": "700", "marginTop": "10px", "marginBottom": "4px", "color": "#1e1b4b"},
                            ),
                            html.Ul(
                                [
                                    html.Li([
                                        html.Span("Run Configuration", style={"fontWeight": "600"}),
                                        " shows your config file defaults on startup. Modify namespace, schema, seeding, and ramp-up here before starting. Click ",
                                        html.Span("Apply configuration", style={"fontWeight": "600"}),
                                        " to save, then ",
                                        html.Span("Start", style={"fontWeight": "600"}),
                                        " to begin.",
                                    ]),
                                    html.Li([
                                        html.Span("Live controls", style={"fontWeight": "600"}),
                                        " (users, workers, think time, operation mix, retry policy) apply immediately to a running test — no restart needed.",
                                    ]),
                                    html.Li([
                                        "Use the ",
                                        html.Span("Validate JSON", style={"fontWeight": "600"}),
                                        " button to check your schema before saving.",
                                    ]),
                                ],
                                style={"marginTop": "4px", "marginBottom": "0", "paddingLeft": "18px", "lineHeight": "1.8"},
                            ),
                            html.Div(
                                "During a test",
                                style={"fontWeight": "700", "marginTop": "12px", "marginBottom": "4px", "color": "#1e1b4b"},
                            ),
                            html.Ul(
                                [
                                    html.Li([
                                        "Run Configuration changes made during an active test are ",
                                        html.Span("staged for the next run", style={"fontWeight": "600"}),
                                        " — the button changes to ",
                                        html.Span("Stage for next run", style={"fontWeight": "600", "color": "#b45309"}),
                                        " to make this clear.",
                                    ]),
                                    html.Li([
                                        html.Span("Scaling workers down", style={"fontWeight": "600"}),
                                        " dismisses excess workers immediately. ",
                                        html.Span("Scaling up", style={"fontWeight": "600"}),
                                        " adds workers gradually (staggered) to avoid thundering-herd connections.",
                                    ]),
                                    html.Li([
                                        "Worker threads are capped at ",
                                        html.Span("max_worker_threads", style={"fontWeight": "600"}),
                                        " (default 500) and cannot exceed the number of virtual users. A visible alert banner appears if either limit is hit.",
                                    ]),
                                    html.Li("If the error rate spikes above 10% after scaling workers, a warning banner appears automatically."),
                                    html.Li("Pause/Resume preserves elapsed time. Paused periods appear as amber overlays on all timeline charts."),
                                ],
                                style={"marginTop": "4px", "marginBottom": "0", "paddingLeft": "18px", "lineHeight": "1.8"},
                            ),
                            html.Div(
                                "Charts & metrics",
                                style={"fontWeight": "700", "marginTop": "12px", "marginBottom": "4px", "color": "#1e1b4b"},
                            ),
                            html.Ul(
                                [
                                    html.Li("Charts and KPIs only update while a test is running. They freeze when the test stops."),
                                    html.Li("Hover the (?) icon next to any chart title for a detailed explanation of what it shows and how to interpret it."),
                                    html.Li([
                                        "The ",
                                        html.Span("Latency Percentiles", style={"fontWeight": "600"}),
                                        " chart shows p50/avg/p95/p99 — when lines diverge, you have a long-tail problem.",
                                    ]),
                                    html.Li([
                                        "The ",
                                        html.Span("Latency Distribution", style={"fontWeight": "600"}),
                                        " histogram reveals bimodal patterns and GC pauses that percentiles alone cannot show.",
                                    ]),
                                    html.Li("Use the timeline filter dropdown to zoom into specific time windows across all charts simultaneously."),
                                ],
                                style={"marginTop": "4px", "marginBottom": "0", "paddingLeft": "18px", "lineHeight": "1.8"},
                            ),
                            html.Div(
                                "Exports & metadata",
                                style={"fontWeight": "700", "marginTop": "12px", "marginBottom": "4px", "color": "#1e1b4b"},
                            ),
                            html.Ul(
                                [
                                    html.Li("Export CSV for raw per-second data, PNG for individual charts, or PDF for a complete report."),
                                    html.Li([
                                        "Each load test is identified by ",
                                        html.Span("load_test_id", style={"fontWeight": "600"}),
                                        " — all config, runs, and events are scoped to this ID in the metadata database.",
                                    ]),
                                    html.Li("Run history (with full metrics summaries) is persisted automatically and can be queried from the metadata collections."),
                                ],
                                style={"marginTop": "4px", "marginBottom": "0", "paddingLeft": "18px", "lineHeight": "1.8"},
                            ),
                        ],
                        style={"fontSize": "12.5px", "color": "#374151"},
                    ),
                ],
                style={
                    **CARD_STYLE,
                    "marginTop": "12px",
                    "background": "#f8fafc",
                    "border": "1px solid #c7d2fe",
                },
                open=False,
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
                            _label_with_help(
                                "Configured duration (seconds)",
                                "How long the active load test runs before auto-stop. Applies immediately.",
                                color="#4b5563",
                                margin_top="8px",
                            ),
                            html.Div(
                                [
                                    dcc.Input(
                                        id="duration-seconds-input",
                                        type="number",
                                        min=1,
                                        step=1,
                                        value=config.load.duration_seconds,
                                        style={
                                            "width": "180px",
                                            "padding": "8px",
                                            "borderRadius": "8px",
                                            "border": "1px solid #d1d5db",
                                        },
                                    ),
                                    html.Button("Apply duration", id="apply-duration-btn", n_clicks=0, style=ACTION_BUTTON_STYLE),
                                    html.Button(
                                        "Reset all settings to defaults",
                                        id="reset-defaults-btn",
                                        n_clicks=0,
                                        title=(
                                            "Reset all live and static controls to default values, "
                                            "and persist those defaults to metadata."
                                        ),
                                        style={
                                            **ACTION_BUTTON_STYLE,
                                            "border": "1px solid #b91c1c",
                                            "background": "#dc2626",
                                            "color": "white",
                                            "fontWeight": "600",
                                        },
                                    ),
                                ],
                                style={"display": "flex", "gap": "8px", "marginTop": "6px", "flexWrap": "wrap"},
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
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Span("Live load shaping"),
                                    _scope_badge("applies now"),
                                ],
                                style={"fontSize": "12px", "color": "#6b7280", "display": "flex", "gap": "8px", "alignItems": "center"},
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            _label_with_help(
                                                "Target users",
                                                "Total simulated users to drive against the cluster. Applies immediately.",
                                            ),
                                            dcc.Input(
                                                id="target-users-input",
                                                type="text",
                                                value=str(config.load.virtual_users),
                                                style={
                                                    "width": "110px",
                                                    "padding": "8px",
                                                    "borderRadius": "8px",
                                                    "border": "1px solid #d1d5db",
                                                    "marginTop": "6px",
                                                },
                                            ),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            _label_with_help(
                                                "Worker threads",
                                                "Concurrent worker loops generating operations. Applies immediately.",
                                            ),
                                            dcc.Input(
                                                id="worker-threads-input",
                                                type="text",
                                                value=str(config.load.worker_threads),
                                                style={
                                                    "width": "110px",
                                                    "padding": "8px",
                                                    "borderRadius": "8px",
                                                    "border": "1px solid #d1d5db",
                                                    "marginTop": "6px",
                                                },
                                            ),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            _label_with_help(
                                                "Think time (ms)",
                                                "Delay range between user operations. Minimum must be <= maximum.",
                                            ),
                                            html.Div(
                                                [
                                                    dcc.Input(
                                                        id="think-time-min-input",
                                                        type="number",
                                                        min=0,
                                                        step=1,
                                                        value=config.load.think_time_ms_min,
                                                        style={
                                                            "width": "90px",
                                                            "padding": "8px",
                                                            "borderRadius": "8px",
                                                            "border": "1px solid #d1d5db",
                                                            "marginTop": "6px",
                                                        },
                                                    ),
                                                    dcc.Input(
                                                        id="think-time-max-input",
                                                        type="number",
                                                        min=0,
                                                        step=1,
                                                        value=config.load.think_time_ms_max,
                                                        style={
                                                            "width": "90px",
                                                            "padding": "8px",
                                                            "borderRadius": "8px",
                                                            "border": "1px solid #d1d5db",
                                                            "marginTop": "6px",
                                                        },
                                                    ),
                                                ],
                                                style={"display": "flex", "gap": "6px"},
                                            ),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            _label_with_help(
                                                "Target ops/user/sec",
                                                "Optional per-user rate cap. Leave empty for unbounded throughput.",
                                            ),
                                            dcc.Input(
                                                id="target-ops-per-user-input",
                                                type="text",
                                                value=(
                                                    ""
                                                    if config.load.target_ops_per_user_per_sec is None
                                                    else f"{config.load.target_ops_per_user_per_sec}"
                                                ),
                                                placeholder="empty = unbounded",
                                                style={
                                                    "width": "150px",
                                                    "padding": "8px",
                                                    "borderRadius": "8px",
                                                    "border": "1px solid #d1d5db",
                                                    "marginTop": "6px",
                                                },
                                            ),
                                        ]
                                    ),
                                ],
                                style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginTop": "8px"},
                            ),
                            html.Button(
                                "Apply load controls",
                                id="apply-load-controls-btn",
                                n_clicks=0,
                                style={**ACTION_BUTTON_STYLE, "marginTop": "10px"},
                            ),
                            html.Div(
                                "Target users are applied instantly up to the provisioned maximum.",
                                style={"marginTop": "6px", "fontSize": "12px", "color": "#6b7280"},
                            ),
                            html.Div(
                                f"External metadata changes can take up to {config.metadata.sync_interval_seconds}s to apply.",
                                style={"marginTop": "4px", "fontSize": "12px", "color": "#6b7280"},
                            ),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Span("Live operation mix (weights)"),
                                    _scope_badge("applies now"),
                                ],
                                style={"fontSize": "12px", "color": "#6b7280", "display": "flex", "gap": "8px", "alignItems": "center"},
                            ),
                            html.Div(
                                [
                                    _weighted_input(
                                        "Read",
                                        "mix-read-input",
                                        config.operation_mix.read,
                                        tooltip="Relative read weight. Higher values increase read share.",
                                    ),
                                    _weighted_input(
                                        "Insert",
                                        "mix-insert-input",
                                        config.operation_mix.insert,
                                        tooltip="Relative insert weight for create-heavy traffic.",
                                    ),
                                    _weighted_input(
                                        "Update",
                                        "mix-update-input",
                                        config.operation_mix.update,
                                        tooltip="Relative update weight for mutable OLTP workloads.",
                                    ),
                                    _weighted_input(
                                        "Delete",
                                        "mix-delete-input",
                                        config.operation_mix.delete,
                                        tooltip="Relative delete weight for cleanup/delete scenarios.",
                                    ),
                                    _weighted_input(
                                        "Find & Modify",
                                        "mix-fam-input",
                                        config.operation_mix.find_and_modify,
                                        tooltip="Relative weight for findAndModify (atomic find+update). Configure targeting in the Schema Builder.",
                                    ),
                                ],
                                style={"display": "grid", "gridTemplateColumns": "repeat(3, 1fr)", "gap": "8px", "marginTop": "8px"},
                            ),
                            html.Button(
                                "Apply operation mix",
                                id="apply-operation-mix-btn",
                                n_clicks=0,
                                style={**ACTION_BUTTON_STYLE, "marginTop": "10px"},
                            ),
                            html.Div(
                                "Values are normalized automatically; at least one weight must be > 0.",
                                style={"marginTop": "6px", "fontSize": "12px", "color": "#6b7280"},
                            ),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Span("Live resilience & logging"),
                                    _scope_badge("applies now"),
                                ],
                                style={"fontSize": "12px", "color": "#6b7280", "display": "flex", "gap": "8px", "alignItems": "center"},
                            ),
                            html.Div(
                                [
                                    _compact_number_input(
                                        "Retry attempts",
                                        "retry-max-attempts-input",
                                        config.retry.max_attempts,
                                        min_value=1,
                                        tooltip="Maximum attempts per operation including the first try.",
                                    ),
                                    _compact_number_input(
                                        "Retry backoff (ms)",
                                        "retry-initial-backoff-input",
                                        config.retry.initial_backoff_ms,
                                        min_value=1,
                                        tooltip="Initial delay before retrying transient failures.",
                                    ),
                                    _compact_number_input(
                                        "Max backoff (ms)",
                                        "retry-max-backoff-input",
                                        config.retry.max_backoff_ms,
                                        min_value=1,
                                        tooltip="Upper bound for exponential retry delay.",
                                    ),
                                    _compact_number_input(
                                        "Backoff multiplier",
                                        "retry-multiplier-input",
                                        config.retry.backoff_multiplier,
                                        step=0.1,
                                        min_value=1,
                                        tooltip="Exponential growth factor for retry backoff.",
                                    ),
                                    _compact_number_input(
                                        "Jitter ratio",
                                        "retry-jitter-input",
                                        config.retry.jitter_ratio,
                                        step=0.05,
                                        min_value=0,
                                        max_value=1,
                                        tooltip="Randomizes backoff to avoid synchronized retry spikes.",
                                    ),
                                    html.Div(
                                        [
                                            _label_with_help(
                                                "Runtime log level",
                                                "Minimum log level emitted to console/file while running.",
                                            ),
                                            dcc.Dropdown(
                                                id="runtime-log-level-input",
                                                options=[
                                                    {"label": "DEBUG", "value": "DEBUG"},
                                                    {"label": "INFO", "value": "INFO"},
                                                    {"label": "WARNING", "value": "WARNING"},
                                                    {"label": "ERROR", "value": "ERROR"},
                                                ],
                                                value=config.logging.level,
                                                clearable=False,
                                                style={"marginTop": "6px"},
                                            ),
                                        ]
                                    ),
                                    _compact_number_input(
                                        "Progress log interval (s)",
                                        "progress-log-interval-input",
                                        config.logging.progress_log_interval_seconds,
                                        min_value=1,
                                        tooltip="How often runner progress summaries are logged.",
                                    ),
                                ],
                                style={"display": "grid", "gridTemplateColumns": "repeat(2, 1fr)", "gap": "8px", "marginTop": "8px"},
                            ),
                            dcc.Checklist(
                                id="log-operation-failures-toggle",
                                options=[
                                    {
                                        "label": html.Span(
                                            "Log failed operations",
                                            title="When enabled, every failed operation is written to logs with details.",
                                        ),
                                        "value": "enabled",
                                    }
                                ],
                                value=["enabled"] if config.logging.log_operation_failures else [],
                                style={"marginTop": "8px"},
                            ),
                            html.Button(
                                "Apply resilience settings",
                                id="apply-resilience-controls-btn",
                                n_clicks=0,
                                style={**ACTION_BUTTON_STYLE, "marginTop": "8px"},
                            ),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "repeat(3, 1fr)", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Span(
                                        "Run Configuration",
                                        style={"fontWeight": "700", "fontSize": "15px", "color": "#1e1b4b"},
                                    ),
                                    _scope_badge("ready", value_id="static-domain-scope-badge"),
                                ],
                                style={"display": "flex", "gap": "10px", "alignItems": "center"},
                            ),
                            html.Div(
                                id="static-config-context-banner",
                                style={"marginTop": "8px"},
                            ),
                            html.Div(
                                id="static-domain-scope-note",
                                style={"display": "none"},
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            _label_with_help(
                                                "Database prefix",
                                                "Prefix used to generate target database names, e.g. prefix_1.",
                                            ),
                                            dcc.Input(
                                                id="namespace-db-prefix-input",
                                                type="text",
                                                value=config.namespace.database_prefix,
                                                style={
                                                    "width": "100%",
                                                    "padding": "8px",
                                                    "borderRadius": "8px",
                                                    "border": "1px solid #d1d5db",
                                                    "marginTop": "6px",
                                                },
                                            ),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            _label_with_help(
                                                "Collection prefix",
                                                "Prefix used to generate collection names per database.",
                                            ),
                                            dcc.Input(
                                                id="namespace-collection-prefix-input",
                                                type="text",
                                                value=config.namespace.collection_prefix,
                                                style={
                                                    "width": "100%",
                                                    "padding": "8px",
                                                    "borderRadius": "8px",
                                                    "border": "1px solid #d1d5db",
                                                    "marginTop": "6px",
                                                },
                                            ),
                                        ]
                                    ),
                                    _compact_number_input(
                                        "Databases",
                                        "namespace-databases-input",
                                        config.namespace.databases,
                                        min_value=1,
                                        tooltip="Number of logical databases targeted by the run.",
                                    ),
                                    _compact_number_input(
                                        "Collections / database",
                                        "namespace-collections-input",
                                        config.namespace.collections_per_database,
                                        min_value=1,
                                        tooltip="Number of collections created/used inside each target database.",
                                    ),
                                    _compact_number_input(
                                        "Initial seed / collection",
                                        "initial-seed-input",
                                        config.load.initial_seed_per_collection,
                                        min_value=0,
                                        tooltip=(
                                            "Number of seed documents inserted into each collection before load starts. "
                                            "Helps reads/updates/deletes behave realistically from second zero."
                                        ),
                                    ),
                                    _compact_number_input(
                                        "Ramp-up seconds",
                                        "ramp-up-seconds-input",
                                        config.load.ramp_up_seconds,
                                        min_value=0,
                                        tooltip="Time window to gradually bring workers online instead of all at once.",
                                    ),
                                ],
                                style={"display": "grid", "gridTemplateColumns": "repeat(3, 1fr)", "gap": "10px", "marginTop": "10px"},
                            ),
                            html.Div(
                                [
                                    html.Button(
                                        "Apply configuration",
                                        id="apply-static-config-btn",
                                        n_clicks=0,
                                        style={**_BTN_BASE, "border": "none", "background": "#059669", "color": "white", "marginRight": "8px"},
                                    ),
                                ],
                                style={"marginTop": "12px", "display": "flex", "alignItems": "center", "gap": "8px"},
                            ),
                            html.Div(
                                id="static-config-feedback",
                                style={"marginTop": "8px", "fontSize": "12px", "color": "#6b7280"},
                            ),
                            html.Div(
                                id="static-staged-indicator",
                                style={
                                    "marginTop": "8px",
                                    "fontSize": "12px",
                                    "padding": "6px 8px",
                                    "borderRadius": "8px",
                                    "fontWeight": "600",
                                },
                            ),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Span(
                                        "Document Schema & Query Targeting",
                                        style={"fontWeight": "700", "fontSize": "15px", "color": "#1e1b4b"},
                                    ),
                                    _scope_badge("ready", value_id="schema-domain-scope-badge"),
                                ],
                                style={"display": "flex", "gap": "10px", "alignItems": "center"},
                            ),
                            dcc.Textarea(
                                id="schema-fields-json-input",
                                value=schema_fields_json_default,
                                style={
                                    "marginTop": "8px",
                                    "width": "100%",
                                    "height": "220px",
                                    "padding": "10px",
                                    "borderRadius": "8px",
                                    "border": "1px solid #d1d5db",
                                    "fontFamily": "'JetBrains Mono', Menlo, Consolas, monospace",
                                    "fontSize": "11.5px",
                                    "lineHeight": "1.5",
                                },
                            ),
                            html.Div(
                                [
                                    html.Button(
                                        "Schema Builder",
                                        id="open-schema-builder-btn",
                                        n_clicks=0,
                                        style={**_BTN_BASE, "border": "none", "background": "#4f46e5", "color": "white"},
                                    ),
                                    html.Button(
                                        "Validate JSON",
                                        id="validate-schema-btn",
                                        n_clicks=0,
                                        style={**_BTN_BASE, "border": "1px solid #6366f1", "background": "#eef2ff", "color": "#4338ca"},
                                    ),
                                    html.Button(
                                        "Reset to defaults",
                                        id="reset-schema-btn",
                                        n_clicks=0,
                                        style={**_BTN_BASE, "border": "1px solid #d1d5db", "background": "#f9fafb", "color": "#6b7280"},
                                    ),
                                ],
                                style={"marginTop": "8px", "display": "flex", "gap": "8px"},
                            ),
                            html.Div(
                                id="schema-validation-feedback",
                                style={"marginTop": "6px", "fontSize": "12px"},
                            ),
                            html.Details(
                                [
                                    html.Summary(
                                        "Schema reference & query targeting guide",
                                        style={"cursor": "pointer", "fontWeight": "600", "color": "#4338ca", "fontSize": "12px"},
                                    ),
                                    html.Div(
                                        [
                                            html.Div("Primitive types", style={"fontWeight": "700", "marginTop": "6px", "color": "#1e1b4b", "fontSize": "12px"}),
                                            _schema_ref_table([
                                                ("string", "length", '{"name":"id","field_type":"string","length":8}'),
                                                ("int", "min_value, max_value", '{"name":"qty","field_type":"int","min_value":1,"max_value":100}'),
                                                ("float", "min_value, max_value", '{"name":"price","field_type":"float","min_value":0.01,"max_value":999}'),
                                                ("decimal", "min_value, max_value", '{"name":"amount","field_type":"decimal","min_value":0,"max_value":9999}'),
                                                ("bool", "—", '{"name":"active","field_type":"bool"}'),
                                                ("choice", "choices[]", '{"name":"status","field_type":"choice","choices":["a","b"]}'),
                                                ("datetime", "date_mode, date_range_days", '{"name":"ts","field_type":"datetime","date_mode":"random_past"}'),
                                                ("objectid", "—", '{"name":"ref","field_type":"objectid"}'),
                                                ("uuid", "—", '{"name":"trace","field_type":"uuid"}'),
                                                ("faker", "faker_provider", '{"name":"email","field_type":"faker","faker_provider":"email"}'),
                                                ("null", "—", '{"name":"deleted_at","field_type":"null"}'),
                                            ]),
                                            html.Div("Nested structures", style={"fontWeight": "700", "marginTop": "10px", "color": "#1e1b4b", "fontSize": "12px"}),
                                            _schema_ref_table([
                                                ("object", "fields[]", "Embedded document with sub-fields"),
                                                ("array", "items, min_items, max_items", "Array of primitives or embedded documents"),
                                            ]),
                                            html.Div("Embedded document example:", style={"fontSize": "11px", "marginTop": "6px", "fontWeight": "600"}),
                                            html.Pre(
                                                '{"name": "address", "field_type": "object", "fields": [\n'
                                                '  {"name": "street", "field_type": "faker", "faker_provider": "street_address"},\n'
                                                '  {"name": "city", "field_type": "faker", "faker_provider": "city"},\n'
                                                '  {"name": "zip", "field_type": "faker", "faker_provider": "zipcode"}\n'
                                                ']}',
                                                style=_PRE_STYLE,
                                            ),
                                            html.Div("Array of strings:", style={"fontSize": "11px", "marginTop": "6px", "fontWeight": "600"}),
                                            html.Pre(
                                                '{"name": "tags", "field_type": "array",\n'
                                                ' "items": {"field_type": "string", "length": 10},\n'
                                                ' "min_items": 1, "max_items": 5}',
                                                style=_PRE_STYLE,
                                            ),
                                            html.Div("Array of embedded documents:", style={"fontSize": "11px", "marginTop": "6px", "fontWeight": "600"}),
                                            html.Pre(
                                                '{"name": "line_items", "field_type": "array",\n'
                                                ' "items": {"field_type": "object", "fields": [\n'
                                                '   {"name": "sku", "field_type": "string", "length": 12},\n'
                                                '   {"name": "qty", "field_type": "int", "min_value": 1, "max_value": 10},\n'
                                                '   {"name": "price", "field_type": "float", "min_value": 1, "max_value": 500}\n'
                                                ' ]},\n'
                                                ' "min_items": 1, "max_items": 8}',
                                                style=_PRE_STYLE,
                                            ),
                                            html.Div("Date modes", style={"fontWeight": "700", "marginTop": "10px", "color": "#1e1b4b", "fontSize": "12px"}),
                                            html.Ul([
                                                html.Li([html.Code("now"), " — current UTC timestamp"]),
                                                html.Li([html.Code("random_past"), " — random date within ", html.Code("date_range_days"), " days ago"]),
                                                html.Li([html.Code("random_future"), " — random date within ", html.Code("date_range_days"), " days ahead"]),
                                                html.Li([html.Code("increment"), " — starts from now, adds ", html.Code("date_increment_seconds"), " per document"]),
                                            ], style={"fontSize": "11px", "paddingLeft": "16px", "marginTop": "2px", "lineHeight": "1.6"}),
                                            html.Div("Faker providers", style={"fontWeight": "700", "marginTop": "10px", "color": "#1e1b4b", "fontSize": "12px"}),
                                            html.Div(
                                                ", ".join(FAKER_PROVIDERS),
                                                style={"fontSize": "10.5px", "color": "#6b7280", "marginTop": "2px", "lineHeight": "1.5"},
                                            ),
                                            html.Div("Query targeting", style={"fontWeight": "700", "marginTop": "10px", "color": "#1e1b4b", "fontSize": "12px"}),
                                            html.Div([
                                                "Add a ", html.Code("query_targets"), " object to control which fields are used for read/update/delete filters "
                                                "instead of _id lookups. Supports dot-notation for nested fields (e.g. ", html.Code('"address.city"'), "). "
                                                "Indexes are created automatically during seeding.",
                                            ], style={"fontSize": "11px", "marginTop": "2px", "lineHeight": "1.5"}),
                                            html.Pre(
                                                '{\n'
                                                '  "fields": [ ... ],\n'
                                                '  "query_targets": {\n'
                                                '    "enabled": true,\n'
                                                '    "read_filter_fields": ["tenant_id", "status"],\n'
                                                '    "update_filter_fields": ["tenant_id"],\n'
                                                '    "delete_filter_fields": ["status"],\n'
                                                '    "create_indexes": true\n'
                                                '  }\n'
                                                '}',
                                                style=_PRE_STYLE,
                                            ),
                                            html.Div([
                                                html.Span("Validation: ", style={"fontWeight": "700"}),
                                                "Duplicate field names at the same level are rejected. "
                                                "Use the Validate JSON button to check before saving.",
                                            ], style={"fontSize": "11px", "marginTop": "8px", "color": "#6b7280", "lineHeight": "1.5"}),
                                        ],
                                        style={"marginTop": "4px"},
                                    ),
                                ],
                                style={"marginTop": "10px"},
                                open=False,
                            ),
                            html.Div(
                                id="schema-domain-scope-note",
                                children="Schema changes are included when you save the run configuration.",
                                style={"marginTop": "8px", "fontSize": "11px", "color": "#9ca3af"},
                            ),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1.25fr 0.75fr", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div(id="live-controls-meta", style={"fontSize": "12px", "color": "#4c1d95"}),
                    html.Ul(
                        id="runtime-change-events",
                        style={"marginTop": "6px", "marginBottom": "0", "paddingLeft": "18px", "fontSize": "12px", "color": "#6b7280"},
                    ),
                ],
                style={**CARD_STYLE, "marginTop": "12px", "background": "#f8fafc", "border": "1px solid #c7d2fe"},
            ),
            html.Div(
                [
                    _profile_chip("Max virtual users", f"{config.load.virtual_users:,}", value_id="max-virtual-users-chip"),
                    _profile_chip(
                        "Target users (live)",
                        f"{config.load.virtual_users:,}",
                        value_id="target-users-chip",
                    ),
                    _profile_chip("Worker threads", f"{config.load.worker_threads:,}", value_id="worker-threads-chip"),
                    _profile_chip(
                        "Configured duration",
                        _format_duration(config.load.duration_seconds),
                        value_id="configured-duration-value",
                    ),
                    _profile_chip(
                        "Namespaces",
                        f"{config.namespace.databases} db x {config.namespace.collections_per_database} coll = {namespace_total}",
                    ),
                    _profile_chip(
                        "Read/Write mix (live)",
                        f"{read_ratio:.1f}% / {write_ratio:.1f}%",
                        value_id="read-write-mix-chip",
                    ),
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
                    _metric_card("P50 latency", "p50-latency", KPI_THEMES[4]),
                    _metric_card("Average latency", "avg-latency", KPI_THEMES[5]),
                    _metric_card("P95 latency", "p95-latency", KPI_THEMES[6]),
                    _metric_card("P99 latency", "p99-latency", KPI_THEMES[7]),
                    _metric_card("Retry rate", "retry-rate", KPI_THEMES[0]),
                    _metric_card("Active users", "active-users-live", KPI_THEMES[1]),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "repeat(5, 1fr)", "gap": "10px"},
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
            html.Div("Throughput & Users", style=_SECTION_HEADER_STYLE),
            html.Div(
                [
                    html.Div(
                        [
                            _chart_title("Operations Throughput", "Total operations per second over time, broken down into read ops and combined write ops (insert + update + delete). Use this to monitor overall cluster throughput and spot drops or plateaus."),
                            dcc.Graph(id="ops-graph", style={"height": "350px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1fr", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div(
                        [
                            _chart_title("Users Ramp View", "Active virtual users vs target users over time. Shows the ramp-up curve and how closely actual concurrency tracks the configured target. Dashed line is the target; solid fill is actual. Paused periods are highlighted in amber."),
                            dcc.Graph(id="users-graph", style={"height": "350px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1fr", "gap": "12px"},
            ),
            html.Div("Latency Analysis", style=_SECTION_HEADER_STYLE),
            html.Div(
                [
                    html.Div(
                        [
                            _chart_title("Latency Percentiles", "Per-second latency distribution: p50 (median), average, p95, and p99 tail latency. When lines are close together, latency is consistent. When p99 diverges from p50, you have a long tail — investigate slow queries, GC pauses, or hot partitions."),
                            dcc.Graph(id="latency-graph", style={"height": "340px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1fr", "gap": "12px"},
            ),
            html.Div("Errors & Reliability", style=_SECTION_HEADER_STYLE),
            html.Div(
                [
                    html.Div(
                        [
                            _chart_title("Errors & Retries", "Error and retry counts per second with error rate percentage on the right axis. Spikes here often correlate with MongoDB failovers, network blips, or connection pool exhaustion. A steady retry count with zero errors means the retry policy is working correctly."),
                            dcc.Graph(id="error-retry-graph", style={"height": "340px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1fr", "gap": "12px"},
            ),
            html.Div("Per-Operation Breakdown", style=_SECTION_HEADER_STYLE),
            html.Div(
                [
                    html.Div(
                        [
                            _chart_title("Throughput by Operation Type", "Stacked area chart showing ops/sec for each operation type individually: read, insert, update, and delete. Useful for verifying the configured operation mix is being applied correctly and spotting if one type is bottlenecking."),
                            dcc.Graph(id="per-op-throughput-graph", style={"height": "340px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1fr", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div(
                        [
                            _chart_title("Latency by Operation Type", "Average latency per operation type over time. Compare read vs write latency to identify if writes are slower (common with write concern majority) or if deletes cause lock contention. Missing lines mean that operation type had no activity in that period."),
                            dcc.Graph(id="per-op-latency-graph", style={"height": "340px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1fr", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div(
                        [
                            _chart_title("Operation Mix", "Donut chart showing the cumulative distribution of operation types for the entire run. Compare this against your configured mix percentages to verify the workload is balanced as expected."),
                            dcc.Graph(id="distribution-graph", style={"height": "330px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            _chart_title("Error Breakdown", "Categorized breakdown of all errors: timeout, network, primary stepdown, server selection, duplicate key, write concern, auth, and other. Helps pinpoint whether errors are infrastructure-related (network/timeout) or application-related (duplicate key/auth)."),
                            dcc.Graph(id="error-breakdown-graph", style={"height": "330px"}),
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
                            _chart_title("Latency Distribution", "Histogram of operation latencies across fixed buckets with a cumulative percentage line. Reveals the shape of your latency distribution — a single peak means consistent performance; multiple peaks or a long tail suggests GC pauses, cold caches, or contention hotspots."),
                            dcc.Graph(id="latency-histogram-graph", style={"height": "340px"}),
                        ],
                        style={**CARD_STYLE},
                    ),
                ],
                style={"marginTop": "12px", "display": "grid", "gridTemplateColumns": "1fr", "gap": "12px"},
            ),
            html.Div(
                [
                    html.Div(
                        [
                            _chart_title("Operation Breakdown", "Per-operation summary table showing count, percentage share, average latency, p95 and p99 tail latency. Use this to compare performance characteristics across read, insert, update, and delete operations."),
                            html.Div(id="operation-table"),
                        ],
                        style={**CARD_STYLE},
                    ),
                    html.Div(
                        [
                            _chart_title("Smart Insights", "Automated analysis of your test results. Flags high error rates (>1%), elevated p95 latency (>50ms), retry rates (>2%), and throughput trends (comparing first half vs second half of the run). Green means healthy; yellow/red means investigate."),
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
                    "background": "#0f172a",
                    "color": "#e2e8f0",
                    "padding": "16px",
                    "borderRadius": "12px",
                    "fontSize": "11.5px",
                    "fontFamily": "'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace",
                    "lineHeight": "1.55",
                    "maxHeight": "380px",
                    "overflowY": "auto",
                    "whiteSpace": "pre-wrap",
                    "border": "1px solid #1e293b",
                    "boxShadow": "inset 0 2px 4px rgba(0,0,0,0.15)",
                },
            ),
            dcc.Download(id="download-csv"),
            dcc.Download(id="download-png"),
            dcc.Download(id="download-pdf"),
            dcc.Interval(id="refresh", interval=config.dashboard.refresh_interval_ms, n_intervals=0),
            dcc.Store(id="schema-builder-fields", data=[]),
            html.Div(
                id="schema-builder-overlay",
                children=[
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Span("Schema Builder", style={"fontWeight": "800", "fontSize": "18px", "color": "#1e1b4b"}),
                                    html.Button(
                                        "X", id="close-schema-builder-btn", n_clicks=0,
                                        style={"background": "none", "border": "none", "fontSize": "20px", "cursor": "pointer", "color": "#6b7280", "fontWeight": "700"},
                                    ),
                                ],
                                style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "12px"},
                            ),
                            html.Div(
                                [
                                    html.Div([
                                        html.Label("Apply schema to", style={"fontSize": "11px", "fontWeight": "600", "color": "#374151"}),
                                        dcc.Dropdown(
                                            id="sb-target-selector",
                                            options=[{"label": "All collections (default schema)", "value": "__all__"}],
                                            value="__all__",
                                            clearable=False,
                                            style={"fontSize": "13px", "marginTop": "3px"},
                                        ),
                                    ], style={"flex": "1"}),
                                    html.Div(
                                        id="sb-target-info",
                                        style={"flex": "1", "fontSize": "11px", "color": "#6b7280", "alignSelf": "end", "paddingBottom": "6px"},
                                    ),
                                ],
                                style={"display": "flex", "gap": "12px", "marginBottom": "14px"},
                            ),
                            html.Div(
                                [
                                    html.Div([
                                        html.Label("Field name (no quotes)", style={"fontSize": "11px", "fontWeight": "600", "color": "#374151"}),
                                        dcc.Input(id="sb-field-name", type="text", placeholder="e.g. tenant_id, address.street",
                                                  style={"width": "100%", "padding": "7px 10px", "borderRadius": "6px", "border": "1px solid #d1d5db", "fontSize": "13px", "marginTop": "3px"}),
                                    ], style={"flex": "1"}),
                                    html.Div([
                                        html.Label("Type", style={"fontSize": "11px", "fontWeight": "600", "color": "#374151"}),
                                        dcc.Dropdown(
                                            id="sb-field-type",
                                            options=[{"label": t, "value": t} for t in FIELD_TYPES],
                                            value="string",
                                            clearable=False,
                                            style={"fontSize": "13px", "marginTop": "3px"},
                                        ),
                                    ], style={"flex": "0.7"}),
                                ],
                                style={"display": "flex", "gap": "10px"},
                            ),
                            html.Div(
                                id="sb-rule-inputs",
                                style={"marginTop": "10px"},
                            ),
                            html.Div(
                                [
                                    html.Button(
                                        "Add field", id="sb-add-field-btn", n_clicks=0,
                                        style={**_BTN_BASE, "border": "none", "background": "#059669", "color": "white"},
                                    ),
                                ],
                                style={"marginTop": "10px"},
                            ),
                            html.Div(id="sb-add-feedback", style={"marginTop": "6px", "fontSize": "12px"}),
                            html.Hr(style={"margin": "16px 0", "border": "none", "borderTop": "1px solid #e5e7eb"}),
                            html.Div(
                                [
                                    html.Span("Fields", style={"fontWeight": "700", "fontSize": "14px", "color": "#1e1b4b"}),
                                    html.Span(id="sb-field-count", style={"fontSize": "12px", "color": "#6b7280", "marginLeft": "8px"}),
                                ],
                                style={"display": "flex", "alignItems": "center", "marginBottom": "8px"},
                            ),
                            html.Div(
                                id="sb-field-list",
                                style={"maxHeight": "300px", "overflowY": "auto"},
                            ),
                            html.Hr(style={"margin": "16px 0", "border": "none", "borderTop": "1px solid #e5e7eb"}),
                            html.Div(
                                [
                                    html.Button(
                                        "Save & apply to schema", id="sb-save-btn", n_clicks=0,
                                        style={**_BTN_BASE, "border": "none", "background": "#4f46e5", "color": "white"},
                                    ),
                                    html.Button(
                                        "Cancel", id="sb-cancel-btn", n_clicks=0,
                                        style={**_BTN_BASE, "border": "1px solid #d1d5db", "background": "#f9fafb", "color": "#6b7280"},
                                    ),
                                    html.Button(
                                        "Load current schema", id="sb-load-current-btn", n_clicks=0,
                                        style={**_BTN_BASE, "border": "1px solid #6366f1", "background": "#eef2ff", "color": "#4338ca"},
                                    ),
                                ],
                                style={"display": "flex", "gap": "8px"},
                            ),
                        ],
                        style={
                            "background": "white",
                            "borderRadius": "16px",
                            "padding": "24px",
                            "width": "680px",
                            "maxHeight": "90vh",
                            "overflowY": "auto",
                            "boxShadow": "0 20px 60px rgba(0,0,0,0.3)",
                        },
                    ),
                ],
                style={
                    "display": "none",
                    "position": "fixed",
                    "top": "0", "left": "0", "right": "0", "bottom": "0",
                    "background": "rgba(0,0,0,0.5)",
                    "zIndex": "9999",
                    "justifyContent": "center",
                    "alignItems": "center",
                },
            ),
        ],
        style={
            "padding": "20px 24px",
            "fontFamily": "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
            "background": "linear-gradient(180deg, #f0f0ff 0%, #f5f3ff 30%, #faf9ff 100%)",
            "minHeight": "100vh",
        },
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
        Output("p50-latency", "children"),
        Output("avg-latency", "children"),
        Output("p95-latency", "children"),
        Output("p99-latency", "children"),
        Output("retry-rate", "children"),
        Output("active-users-live", "children"),
        Output("ops-graph", "figure"),
        Output("latency-graph", "figure"),
        Output("users-graph", "figure"),
        Output("error-retry-graph", "figure"),
        Output("per-op-throughput-graph", "figure"),
        Output("per-op-latency-graph", "figure"),
        Output("distribution-graph", "figure"),
        Output("error-breakdown-graph", "figure"),
        Output("latency-histogram-graph", "figure"),
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
            persist_run_completion_if_needed(snapshot)
            snapshot = _filter_snapshot_to_run_start(snapshot, runner.started_at)
            window_seconds = int(timeline_window or 0)
            filter_reference = snapshot.generated_at if runner.is_running else runner.finished_at
            filtered_snapshot = _filter_snapshot_timeline(snapshot, window_seconds, reference_time=filter_reference)
            filtered_snapshot = _remove_incomplete_live_second(filtered_snapshot, runner.is_running)
            x_range = _timeline_x_range(
                window_seconds, snapshot.generated_at, runner.started_at,
                is_running=runner.is_running, finished_at=runner.finished_at,
            )
            stable_ops_per_sec = _compute_stable_ops_per_sec(snapshot.total_ops, runner.active_elapsed_seconds())
            error_rate = (snapshot.error_ops / snapshot.total_ops * 100.0) if snapshot.total_ops else 0.0
            success_rate = (snapshot.success_ops / snapshot.total_ops * 100.0) if snapshot.total_ops else 0.0
            retry_rate = (snapshot.retried_ops / snapshot.total_ops * 100.0) if snapshot.total_ops else 0.0
            status_text, status_style = _build_status_ui(runner)
            runtime_meta, progress_value, progress_label = _build_runtime_ui(runner)
            if runner.is_running:
                last_updated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            elif runner.finished_at is not None:
                last_updated = runner.finished_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                last_updated = "Waiting for test to start"
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
                f"{snapshot.p50_latency_ms:.2f} ms",
                f"{snapshot.avg_latency_ms:.2f} ms",
                f"{snapshot.p95_latency_ms:.2f} ms",
                f"{snapshot.p99_latency_ms:.2f} ms",
                f"{retry_rate:.2f}%",
                _fmt(snapshot.active_virtual_users_current),
                build_operations_timeline(filtered_snapshot, x_range=x_range),
                build_latency_timeline(filtered_snapshot, x_range=x_range),
                build_user_ramp_timeline(filtered_snapshot, x_range=x_range),
                build_error_retry_timeline(filtered_snapshot, x_range=x_range),
                build_per_op_throughput_timeline(filtered_snapshot, x_range=x_range),
                build_per_op_latency_timeline(filtered_snapshot, x_range=x_range),
                build_operation_distribution(snapshot),
                build_error_breakdown(snapshot),
                build_latency_histogram(snapshot),
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

    _SCALING_ALERT_DISPLAY_SECONDS = 8
    _scaling_alert_state: dict[str, object] = {"messages": [], "show_until": 0.0}

    _ALERT_VISIBLE_STYLE = {
        "marginTop": "12px",
        "padding": "14px 18px",
        "background": "linear-gradient(90deg, #fef2f2, #fff1f2)",
        "border": "2px solid #f87171",
        "borderRadius": "12px",
        "fontSize": "13px",
        "color": "#991b1b",
        "fontWeight": "500",
        "boxShadow": "0 4px 12px rgba(239,68,68,0.15)",
        "animation": "pulse 2s ease-in-out 3",
    }

    @callback(
        Output("scaling-alert-banner", "children"),
        Output("scaling-alert-banner", "style"),
        Input("refresh", "n_intervals"),
    )
    def refresh_scaling_alerts(_: int):
        import time as _time
        now = _time.monotonic()
        new_alerts = runner.drain_scaling_alerts()
        if new_alerts:
            _scaling_alert_state["messages"] = new_alerts
            _scaling_alert_state["show_until"] = now + _SCALING_ALERT_DISPLAY_SECONDS
        if now >= float(_scaling_alert_state["show_until"]):
            return "", {"display": "none"}
        messages = _scaling_alert_state["messages"]
        if not messages:
            return "", {"display": "none"}
        alert_items = [
            html.Div(
                [
                    html.Span(
                        "SCALING WARNING",
                        style={
                            "fontWeight": "800",
                            "fontSize": "12px",
                            "letterSpacing": "0.05em",
                            "marginRight": "10px",
                            "background": "#dc2626",
                            "color": "white",
                            "padding": "2px 8px",
                            "borderRadius": "4px",
                        },
                    ),
                    html.Span(alert),
                ],
                style={"marginBottom": "4px" if i < len(messages) - 1 else "0"},
            )
            for i, alert in enumerate(messages)
        ]
        return alert_items, _ALERT_VISIBLE_STYLE

    @callback(
        Output("configured-duration-value", "children"),
        Output("target-users-chip", "children"),
        Output("read-write-mix-chip", "children"),
        Output("live-controls-meta", "children"),
        Output("runtime-change-events", "children"),
        Input("refresh", "n_intervals"),
    )
    def refresh_live_control_state(_: int):
        try:
            settings = controller.live_settings_snapshot()
            duration_seconds = int(settings["target_duration_seconds"])
            max_users = int(settings["max_virtual_users"])
            target_users = int(settings["target_virtual_users"])
            worker_threads = int(settings["target_worker_threads"])
            worker_pool = int(settings["worker_thread_pool_size"])
            operation_mix = settings["operation_mix"]
            retry_policy = settings["retry_policy"]
            read_pct = operation_mix["read"] * 100
            write_pct = (operation_mix["insert"] + operation_mix["update"] + operation_mix["delete"]) * 100
            target_rate = settings["target_ops_per_user_per_sec"]
            target_rate_text = "unbounded" if target_rate is None else f"{float(target_rate):.2f}/sec"
            live_meta = (
                f"Live state: users {target_users}/{max_users} | think {settings['think_time_ms_min']}-"
                f"{settings['think_time_ms_max']} ms | target ops/user {target_rate_text} | "
                f"workers {worker_threads} (pool {worker_pool}) | "
                f"retry attempts {retry_policy['max_attempts']} | "
                f"log level {settings['log_level']} | progress logs every {settings['progress_log_interval_seconds']}s"
            )
            if metadata_store is not None:
                live_meta += (
                    f" | load test id {metadata_store.load_test_id} | "
                    f"metadata sync every {config.metadata.sync_interval_seconds}s"
                )
            events = controller.runtime_change_events(limit=8)
            if events:
                event_items = [
                    html.Li(f"{event.timestamp.strftime('%H:%M:%S UTC')} - {event.message}")
                    for event in reversed(events)
                ]
            else:
                event_items = [html.Li("No live runtime changes have been applied yet.")]
            return (
                _format_duration(duration_seconds),
                _fmt(target_users),
                f"{read_pct:.1f}% / {write_pct:.1f}%",
                live_meta,
                event_items,
            )
        except Exception as exc:  # pragma: no cover - UI safety
            logger.exception("Live control state refresh failed.")
            return (
                _format_duration(runner.target_duration_seconds),
                _fmt(runner.target_virtual_users),
                "N/A",
                f"Live settings unavailable: {exc}",
                [html.Li("Unable to load runtime change history.")],
            )

    @callback(
        Output("max-virtual-users-chip", "children"),
        Output("worker-threads-chip", "children"),
        Input("refresh", "n_intervals"),
    )
    def refresh_capacity_chips(_: int):
        try:
            settings = controller.live_settings_snapshot()
            return _fmt(int(settings["max_virtual_users"])), _fmt(int(settings["target_worker_threads"]))
        except Exception:  # pragma: no cover - UI safety
            logger.exception("Capacity chip refresh failed.")
            return _fmt(runner.max_virtual_users), _fmt(runner.target_worker_threads)

    @callback(
        Output("start-test-btn", "disabled"),
        Input("refresh", "n_intervals"),
    )
    def refresh_start_button_state(_: int):
        return runner.is_running

    @callback(
        Output("static-domain-scope-badge", "children"),
        Output("static-domain-scope-badge", "style"),
        Output("schema-domain-scope-badge", "children"),
        Output("schema-domain-scope-badge", "style"),
        Output("static-config-context-banner", "children"),
        Output("static-config-context-banner", "style"),
        Output("apply-static-config-btn", "children"),
        Output("apply-static-config-btn", "style"),
        Output("static-domain-scope-note", "children"),
        Output("schema-domain-scope-note", "children"),
        Input("refresh", "n_intervals"),
    )
    def refresh_static_scope_state(_: int):
        _banner_base = {
            "padding": "10px 14px",
            "borderRadius": "10px",
            "fontSize": "12.5px",
            "lineHeight": "1.5",
        }
        if runner.is_running:
            label = "STAGED"
            style = _scope_badge_style("next_run")
            banner = html.Div([
                html.Span("Test in progress. ", style={"fontWeight": "700"}),
                "Any changes saved here will be ",
                html.Span("staged for the next run", style={"fontWeight": "700"}),
                " and will not affect the currently running test. "
                "They will take effect when you stop and restart, or start a new test.",
            ])
            banner_style = {**_banner_base, "background": "#fef3c7", "border": "1px solid #fbbf24", "color": "#78350f"}
            btn_text = "Stage for next run"
            btn_style = {**_BTN_BASE, "border": "none", "background": "#f59e0b", "color": "white", "marginRight": "8px"}
            return label, style, label, style, banner, banner_style, btn_text, btn_style, "", ""

        finished = runner.finished_at is not None
        startup_baseline = run_state.get("startup_static_baseline")
        current_snapshot = _static_config_snapshot(controller.current_config())
        has_customizations = isinstance(startup_baseline, dict) and current_snapshot != startup_baseline

        if finished:
            if has_customizations:
                label = "MODIFIED"
                style = _scope_badge_style("user_config")
                banner = html.Div([
                    html.Span("Previous test completed. ", style={"fontWeight": "700"}),
                    "You have modified settings from the defaults. "
                    "These changes will be applied when you click ",
                    html.Span("Start", style={"fontWeight": "700"}),
                    " to begin a new test.",
                ])
            else:
                label = "READY"
                style = _scope_badge_style("active_default")
                banner = html.Div([
                    html.Span("Previous test completed. ", style={"fontWeight": "700"}),
                    "Current settings reflect your configuration defaults. "
                    "Modify them here before starting the next test, or click ",
                    html.Span("Start", style={"fontWeight": "700"}),
                    " to run with these values.",
                ])
            banner_style = {**_banner_base, "background": "#dbeafe", "border": "1px solid #93c5fd", "color": "#1e3a5f"}
        elif has_customizations:
            label = "MODIFIED"
            style = _scope_badge_style("user_config")
            banner = html.Div([
                html.Span("Settings modified. ", style={"fontWeight": "700"}),
                "You have changed values from the configuration defaults. "
                "Click ",
                html.Span("Apply configuration", style={"fontWeight": "700"}),
                " to save, then click ",
                html.Span("Start", style={"fontWeight": "700"}),
                " to begin the load test with these settings.",
            ])
            banner_style = {**_banner_base, "background": "#e0f2fe", "border": "1px solid #7dd3fc", "color": "#0c4a6e"}
        else:
            label = "DEFAULTS"
            style = _scope_badge_style("active_default")
            banner = html.Div([
                html.Span("Default configuration loaded. ", style={"fontWeight": "700"}),
                "These values come from your config file. "
                "You can modify them here before starting your load test. "
                "Click ",
                html.Span("Apply configuration", style={"fontWeight": "700"}),
                " to save changes, then click ",
                html.Span("Start", style={"fontWeight": "700"}),
                " when ready.",
            ])
            banner_style = {**_banner_base, "background": "#ecfdf5", "border": "1px solid #86efac", "color": "#14532d"}

        btn_text = "Apply configuration"
        btn_style = {**_BTN_BASE, "border": "none", "background": "#059669", "color": "white", "marginRight": "8px"}
        return label, style, label, style, banner, banner_style, btn_text, btn_style, "", ""

    @callback(
        Output("static-staged-indicator", "children"),
        Output("static-staged-indicator", "style"),
        Input("refresh", "n_intervals"),
    )
    def refresh_static_staged_indicator(_: int):
        base_style = {
            "marginTop": "8px",
            "fontSize": "12px",
            "padding": "6px 8px",
            "borderRadius": "8px",
            "fontWeight": "600",
        }
        if not runner.is_running:
            run_state["run_static_baseline"] = None
            run_state["staged_static_snapshot"] = None
            startup_baseline = run_state.get("startup_static_baseline")
            current_snapshot = _static_config_snapshot(controller.current_config())
            if isinstance(startup_baseline, dict):
                startup_diff = _static_config_diff_labels(current_snapshot, startup_baseline)
                if startup_diff:
                    preview = ", ".join(startup_diff[:3])
                    if len(startup_diff) > 3:
                        preview += f" +{len(startup_diff) - 3} more"
                    return (
                        f"{len(startup_diff)} startup customization(s) selected: {preview}.",
                        {**base_style, "background": "#e0f2fe", "border": "1px solid #7dd3fc", "color": "#0c4a6e"},
                    )
            return (
                "No staged static changes pending next run.",
                {**base_style, "background": "#ecfdf5", "border": "1px solid #86efac", "color": "#166534"},
            )
        baseline = run_state.get("run_static_baseline")
        staged_snapshot = run_state.get("staged_static_snapshot")
        current_snapshot = _static_config_snapshot(controller.current_config())
        compare_snapshot = staged_snapshot if isinstance(staged_snapshot, dict) else current_snapshot
        if baseline is None:
            run_state["run_static_baseline"] = current_snapshot
            return (
                "No staged static changes pending next run.",
                {**base_style, "background": "#ecfdf5", "border": "1px solid #86efac", "color": "#166534"},
            )
        diff_labels = _static_config_diff_labels(compare_snapshot, baseline)
        if not diff_labels:
            return (
                "No staged static changes pending next run.",
                {**base_style, "background": "#ecfdf5", "border": "1px solid #86efac", "color": "#166534"},
            )
        preview = ", ".join(diff_labels[:3])
        if len(diff_labels) > 3:
            preview += f" +{len(diff_labels) - 3} more"
        message = f"{len(diff_labels)} staged change(s) pending next run: {preview}."
        return (
            message,
            {**base_style, "background": "#fffbeb", "border": "1px solid #f59e0b", "color": "#92400e"},
        )

    # Compatibility callback: supports older clients that still request a single
    # 7-output live-control update signature.
    @callback(
        Output("configured-duration-value", "children", allow_duplicate=True),
        Output("max-virtual-users-chip", "children", allow_duplicate=True),
        Output("target-users-chip", "children", allow_duplicate=True),
        Output("worker-threads-chip", "children", allow_duplicate=True),
        Output("read-write-mix-chip", "children", allow_duplicate=True),
        Output("live-controls-meta", "children", allow_duplicate=True),
        Output("runtime-change-events", "children", allow_duplicate=True),
        Input("refresh", "n_intervals"),
        prevent_initial_call=True,
    )
    def refresh_live_control_state_legacy(_: int):
        duration, target_users, read_write_mix, live_meta, events = refresh_live_control_state(_)
        max_users, worker_threads = refresh_capacity_chips(_)
        return (
            duration,
            max_users,
            target_users,
            worker_threads,
            read_write_mix,
            live_meta,
            events,
        )

    @callback(
        Output("control-feedback", "children"),
        Input("start-test-btn", "n_clicks"),
        Input("stop-test-btn", "n_clicks"),
        Input("pause-test-btn", "n_clicks"),
        Input("resume-test-btn", "n_clicks"),
        Input("restart-test-btn", "n_clicks"),
        Input("apply-duration-btn", "n_clicks"),
        Input("apply-load-controls-btn", "n_clicks"),
        Input("apply-operation-mix-btn", "n_clicks"),
        Input("apply-resilience-controls-btn", "n_clicks"),
        State("duration-seconds-input", "value"),
        State("target-users-input", "value"),
        State("worker-threads-input", "value"),
        State("think-time-min-input", "value"),
        State("think-time-max-input", "value"),
        State("target-ops-per-user-input", "value"),
        State("mix-read-input", "value"),
        State("mix-insert-input", "value"),
        State("mix-update-input", "value"),
        State("mix-delete-input", "value"),
        State("mix-fam-input", "value"),
        State("retry-max-attempts-input", "value"),
        State("retry-initial-backoff-input", "value"),
        State("retry-max-backoff-input", "value"),
        State("retry-multiplier-input", "value"),
        State("retry-jitter-input", "value"),
        State("runtime-log-level-input", "value"),
        State("progress-log-interval-input", "value"),
        State("log-operation-failures-toggle", "value"),
        prevent_initial_call=True,
    )
    def control_runner(
        start_clicks: int,
        stop_clicks: int,
        pause_clicks: int,
        resume_clicks: int,
        restart_clicks: int,
        apply_clicks: int,
        apply_load_clicks: int,
        apply_mix_clicks: int,
        apply_resilience_clicks: int,
        duration_value: int,
        target_users_value: str,
        worker_threads_value: str,
        think_min_value: int,
        think_max_value: int,
        target_ops_per_user_value: str,
        mix_read_value: float,
        mix_insert_value: float,
        mix_update_value: float,
        mix_delete_value: float,
        mix_fam_value: float,
        retry_max_attempts_value: int,
        retry_initial_backoff_value: int,
        retry_max_backoff_value: int,
        retry_multiplier_value: float,
        retry_jitter_value: float,
        runtime_log_level_value: str,
        progress_log_interval_value: int,
        log_operation_failures_value: list[str] | None,
    ):
        triggered = ctx.triggered_id
        if triggered is None:
            raise PreventUpdate
        try:
            if triggered == "start-test-btn":
                if not start_clicks:
                    raise PreventUpdate
                apply_latest_metadata_config_before_start()
                persist_metadata_config("start-run")
                started = controller.start()
                if not started:
                    return _feedback_warning("Start ignored because a run is already active.")
                if metadata_store is not None:
                    config_version = int(run_state.get("config_version") or 1)
                    run_id = metadata_store.record_run_started(config_version=config_version)
                    run_state["active_run_id"] = run_id
                    run_state["last_run_finished_at"] = None
                    metadata_store.append_event(
                        event_type="control_action",
                        message="Start requested from dashboard.",
                        payload={"action": "start", "run_id": run_id},
                    )
                run_state["run_static_baseline"] = _static_config_snapshot(controller.current_config())
                run_state["staged_static_snapshot"] = None
                return _feedback_info("Load test started from dashboard.")
            if triggered == "stop-test-btn":
                if not stop_clicks:
                    raise PreventUpdate
                controller.stop()
                if metadata_store is not None:
                    metadata_store.append_event(
                        event_type="control_action",
                        message="Stop requested from dashboard.",
                        payload={"action": "stop", "run_id": run_state.get("active_run_id")},
                    )
                return _feedback_info("Stop requested. Workers are finishing in-flight operations.")
            if triggered == "pause-test-btn":
                if not pause_clicks:
                    raise PreventUpdate
                paused = controller.pause()
                if paused and metadata_store is not None:
                    metadata_store.append_event(
                        event_type="control_action",
                        message="Pause requested from dashboard.",
                        payload={"action": "pause", "run_id": run_state.get("active_run_id")},
                    )
                return _feedback_info("Load test paused.") if paused else _feedback_warning(
                    "Pause ignored (runner is not active)."
                )
            if triggered == "resume-test-btn":
                if not resume_clicks:
                    raise PreventUpdate
                resumed = controller.resume()
                if resumed and metadata_store is not None:
                    metadata_store.append_event(
                        event_type="control_action",
                        message="Resume requested from dashboard.",
                        payload={"action": "resume", "run_id": run_state.get("active_run_id")},
                    )
                return _feedback_info("Load test resumed.") if resumed else _feedback_warning(
                    "Resume ignored (runner is not paused)."
                )
            if triggered == "restart-test-btn":
                if not restart_clicks:
                    raise PreventUpdate
                apply_latest_metadata_config_before_start()
                restarted, message = controller.restart()
                if restarted and metadata_store is not None:
                    persist_metadata_config("restart")
                    config_version = int(run_state.get("config_version") or 1)
                    run_id = metadata_store.record_run_started(config_version=config_version)
                    run_state["active_run_id"] = run_id
                    run_state["last_run_finished_at"] = None
                if restarted:
                    run_state["run_static_baseline"] = _static_config_snapshot(controller.current_config())
                    run_state["staged_static_snapshot"] = None
                return _feedback_info(message)
            if triggered == "apply-duration-btn":
                if not apply_clicks:
                    raise PreventUpdate
                if duration_value is None:
                    return _feedback_warning("Duration value is required.")
                updated = controller.update_duration_seconds(int(duration_value))
                persist_metadata_config("apply-duration")
                return _feedback_info(f"Run duration updated to {updated} seconds.")
            if triggered == "apply-load-controls-btn":
                if not apply_load_clicks:
                    raise PreventUpdate
                think_min_ms, think_max_ms = _validate_think_time_values(think_min_value, think_max_value)
                target_users_requested = _parse_positive_int(target_users_value, "Target users")
                worker_threads_requested = _parse_positive_int(worker_threads_value, "Worker threads")
                target_rate = _parse_optional_positive_float(target_ops_per_user_value, "Target ops/user/sec")
                target_users, max_users = controller.update_target_virtual_users(target_users_requested)
                worker_threads, worker_pool = controller.update_worker_threads(worker_threads_requested)
                min_ms, max_ms, applied_rate = controller.update_pacing(
                    think_time_ms_min=think_min_ms,
                    think_time_ms_max=think_max_ms,
                    target_ops_per_user_per_sec=target_rate,
                )
                rate_text = "unbounded" if applied_rate is None else f"{applied_rate:.2f} ops/user/sec"
                pool_note = ""
                if worker_pool < worker_threads:
                    pool_note = (
                        f" (worker pool currently {worker_pool}; scaling in progress)"
                    )
                persist_metadata_config("apply-load-controls")
                was_capped = worker_threads < worker_threads_requested
                msg = (
                    f"Live load controls applied: users {target_users}/{max_users}, "
                    f"worker threads {worker_threads}{pool_note}, "
                    f"think time {min_ms}-{max_ms} ms, target ops/user {rate_text}."
                )
                if was_capped:
                    msg += (
                        f" Worker threads were capped from {worker_threads_requested} to {worker_threads}."
                    )
                    if worker_threads_requested > max_users:
                        msg += f" Increase target users (currently {max_users}) to allow more workers."
                    return _feedback_warning(msg)
                return _feedback_info(msg)
            if triggered == "apply-operation-mix-btn":
                if not apply_mix_clicks:
                    raise PreventUpdate
                if None in (mix_read_value, mix_insert_value, mix_update_value, mix_delete_value):
                    return _feedback_warning("All operation mix values are required.")
                fam = float(mix_fam_value or 0)
                normalized = controller.update_operation_mix(
                    read=float(mix_read_value),
                    insert=float(mix_insert_value),
                    update=float(mix_update_value),
                    delete=float(mix_delete_value),
                    find_and_modify=fam,
                )
                persist_metadata_config("apply-operation-mix")
                return _feedback_info(
                    "Operation mix updated to "
                    f"read {normalized['read'] * 100:.1f}%, "
                    f"insert {normalized['insert'] * 100:.1f}%, "
                    f"update {normalized['update'] * 100:.1f}%, "
                    f"delete {normalized['delete'] * 100:.1f}%, "
                    f"findAndModify {normalized.get('find_and_modify', 0) * 100:.1f}%."
                )
            if triggered == "apply-resilience-controls-btn":
                if not apply_resilience_clicks:
                    raise PreventUpdate
                required_retry_values = (
                    retry_max_attempts_value,
                    retry_initial_backoff_value,
                    retry_max_backoff_value,
                    retry_multiplier_value,
                    retry_jitter_value,
                    progress_log_interval_value,
                )
                if any(value is None for value in required_retry_values):
                    return _feedback_warning("Retry and runtime logging values are required.")
                retry_values = controller.update_retry_policy(
                    max_attempts=int(retry_max_attempts_value),
                    initial_backoff_ms=int(retry_initial_backoff_value),
                    max_backoff_ms=int(retry_max_backoff_value),
                    backoff_multiplier=float(retry_multiplier_value),
                    jitter_ratio=float(retry_jitter_value),
                )
                log_operation_failures = bool(log_operation_failures_value and "enabled" in log_operation_failures_value)
                level, interval, failures = controller.update_runtime_logging(
                    level_name=runtime_log_level_value or "INFO",
                    progress_log_interval_seconds=int(progress_log_interval_value),
                    log_operation_failures=log_operation_failures,
                )
                persist_metadata_config("apply-resilience-controls")
                return _feedback_info(
                    "Resilience settings updated: "
                    f"retry attempts={retry_values[0]}, "
                    f"backoff={retry_values[1]}-{retry_values[2]} ms, "
                    f"multiplier={retry_values[3]:.2f}, jitter={retry_values[4]:.2f}; "
                    f"log level={level}, progress logs every {interval}s, "
                    f"operation failure logging={'on' if failures else 'off'}."
                )
            raise PreventUpdate
        except ValueError as exc:
            logger.warning("Runner control validation rejected input: %s", exc)
            return _feedback_warning(str(exc))
        except Exception as exc:  # pragma: no cover - UI safety
            logger.exception("Runner control callback failed.")
            return _feedback_error(f"Control action failed: {exc}")

    @callback(
        Output("control-feedback", "children", allow_duplicate=True),
        Output("static-config-feedback", "children"),
        Input("apply-static-config-btn", "n_clicks"),
        State("namespace-db-prefix-input", "value"),
        State("namespace-collection-prefix-input", "value"),
        State("namespace-databases-input", "value"),
        State("namespace-collections-input", "value"),
        State("initial-seed-input", "value"),
        State("ramp-up-seconds-input", "value"),
        State("schema-fields-json-input", "value"),
        prevent_initial_call=True,
    )
    def apply_static_config(
        apply_static_clicks: int,
        namespace_db_prefix_value: str,
        namespace_collection_prefix_value: str,
        namespace_databases_value: int,
        namespace_collections_value: int,
        initial_seed_value: int,
        ramp_up_seconds_value: int,
        schema_fields_json_value: str,
    ):
        if not apply_static_clicks:
            raise PreventUpdate
        try:
            namespace_db_prefix = _parse_non_empty_text(namespace_db_prefix_value, "Database prefix")
            namespace_collection_prefix = _parse_non_empty_text(
                namespace_collection_prefix_value,
                "Collection prefix",
            )
            namespace_databases = _parse_positive_int(namespace_databases_value, "Namespace databases")
            namespace_collections = _parse_positive_int(
                namespace_collections_value,
                "Collections per database",
            )
            initial_seed = _parse_non_negative_int(initial_seed_value, "Initial seed per collection")
            ramp_up_seconds = _parse_non_negative_int(ramp_up_seconds_value, "Ramp-up seconds")
            parsed_schema = _parse_schema_config(schema_fields_json_value)
            staged_config = controller.current_config()
            staged_config.namespace.database_prefix = namespace_db_prefix
            staged_config.namespace.collection_prefix = namespace_collection_prefix
            staged_config.namespace.databases = namespace_databases
            staged_config.namespace.collections_per_database = namespace_collections
            staged_config.load.initial_seed_per_collection = initial_seed
            staged_config.load.ramp_up_seconds = ramp_up_seconds
            staged_config.document_schema = parsed_schema
            if runner.is_running:
                run_state["staged_static_snapshot"] = _static_config_snapshot(staged_config)
                persist_metadata_config(
                    "apply-static-config-running",
                    config_override=staged_config,
                    mark_synced=False,
                )
                message = (
                    "Static config saved and staged for next run. Namespace/schema/seed/ramp-up changes "
                    "apply on next restart/start."
                )
                return _feedback_warning(message), message
            controller.apply_config(staged_config, source="dashboard-static-config")
            run_state["staged_static_snapshot"] = None
            persist_metadata_config("apply-static-config")
            message = "Static config saved. Changes are staged and will apply when you start or restart the load test."
            return _feedback_info(message), message
        except ValueError as exc:
            logger.warning("Static config validation rejected input: %s", exc)
            message = str(exc)
            return _feedback_warning(message), message
        except Exception as exc:  # pragma: no cover - UI safety
            logger.exception("Static config callback failed.")
            message = f"Static config action failed: {exc}"
            return _feedback_error(message), message

    @callback(
        Output("schema-validation-feedback", "children"),
        Input("validate-schema-btn", "n_clicks"),
        State("schema-fields-json-input", "value"),
        prevent_initial_call=True,
    )
    def validate_schema_json(n_clicks: int, schema_json_value: str):
        if not n_clicks:
            raise PreventUpdate
        try:
            parsed = _parse_schema_config(schema_json_value)
            field_count = len(parsed.fields)
            field_names = ", ".join(f.name for f in parsed.fields[:5])
            if field_count > 5:
                field_names += f" +{field_count - 5} more"
            return html.Div(
                [
                    html.Span(
                        "VALID",
                        style={
                            "fontWeight": "800", "fontSize": "11px", "letterSpacing": "0.05em",
                            "marginRight": "8px", "background": "#059669", "color": "white",
                            "padding": "2px 8px", "borderRadius": "4px",
                        },
                    ),
                    html.Span(f"{field_count} field(s): {field_names}"),
                ],
                style={
                    "padding": "8px 12px", "borderRadius": "8px",
                    "background": "#ecfdf5", "border": "1px solid #86efac",
                    "color": "#166534",
                },
            )
        except Exception as exc:
            error_msg = str(exc)
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            return html.Div(
                [
                    html.Span(
                        "INVALID",
                        style={
                            "fontWeight": "800", "fontSize": "11px", "letterSpacing": "0.05em",
                            "marginRight": "8px", "background": "#dc2626", "color": "white",
                            "padding": "2px 8px", "borderRadius": "4px",
                        },
                    ),
                    html.Span(error_msg),
                ],
                style={
                    "padding": "8px 12px", "borderRadius": "8px",
                    "background": "#fef2f2", "border": "1px solid #fca5a5",
                    "color": "#991b1b",
                },
            )

    @callback(
        Output("schema-fields-json-input", "value", allow_duplicate=True),
        Output("schema-validation-feedback", "children", allow_duplicate=True),
        Input("reset-schema-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def reset_schema_to_defaults(n_clicks: int):
        if not n_clicks:
            raise PreventUpdate
        default_schema = default_config().document_schema
        return _schema_config_to_json(default_schema), _feedback_info("Schema reset to defaults.")

    @callback(
        Output("schema-builder-overlay", "style"),
        Output("schema-builder-fields", "data"),
        Output("sb-target-selector", "options"),
        Output("sb-target-info", "children"),
        Input("open-schema-builder-btn", "n_clicks"),
        Input("close-schema-builder-btn", "n_clicks"),
        Input("sb-cancel-btn", "n_clicks"),
        State("schema-builder-fields", "data"),
        prevent_initial_call=True,
    )
    def toggle_schema_builder(open_clicks, close_clicks, cancel_clicks, current_fields):
        _overlay_visible = {
            "display": "flex",
            "position": "fixed",
            "top": "0", "left": "0", "right": "0", "bottom": "0",
            "background": "rgba(0,0,0,0.5)",
            "zIndex": "9999",
            "justifyContent": "center",
            "alignItems": "center",
        }
        _overlay_hidden = {"display": "none"}
        triggered = ctx.triggered_id
        if triggered == "open-schema-builder-btn":
            fields = current_fields if current_fields else []
            current_config = controller.current_config()
            ns = current_config.namespace
            options = [{"label": "All collections (default schema)", "value": "__all__"}]
            for db_idx in range(ns.databases):
                db_name = f"{ns.database_prefix}_{db_idx + 1}"
                for coll_idx in range(ns.collections_per_database):
                    coll_name = f"{ns.collection_prefix}_{coll_idx + 1}"
                    target = f"{db_name}.{coll_name}"
                    has_custom = runner.schema_registry.has_override(db_name, coll_name)
                    label = f"{target} (custom)" if has_custom else target
                    options.append({"label": label, "value": target})
            overrides = runner.schema_registry.all_overrides()
            info = f"{len(overrides)} collection(s) with custom schemas" if overrides else "All collections use the default schema"
            return _overlay_visible, fields, options, info
        return _overlay_hidden, current_fields or [], no_update, no_update

    @callback(
        Output("sb-rule-inputs", "children"),
        Input("sb-field-type", "value"),
    )
    def update_rule_inputs(field_type):
        _input_style = {"width": "100%", "padding": "6px 10px", "borderRadius": "6px", "border": "1px solid #d1d5db", "fontSize": "12px", "marginTop": "3px"}
        _label_style = {"fontSize": "11px", "fontWeight": "600", "color": "#374151"}
        if field_type in ("string",):
            return html.Div([
                html.Div([
                    html.Div([html.Label("Length", style=_label_style), dcc.Input(id="sb-rule-length", type="number", value=16, min=1, style=_input_style)], style={"flex": "0.5"}),
                    html.Div([html.Label("Prefix (optional)", style=_label_style), dcc.Input(id="sb-rule-prefix", type="text", placeholder="e.g. ORD-", style=_input_style)], style={"flex": "1"}),
                    html.Div([html.Label("Suffix (optional)", style=_label_style), dcc.Input(id="sb-rule-suffix", type="text", placeholder="e.g. -US", style=_input_style)], style={"flex": "1"}),
                ], style={"display": "flex", "gap": "8px"}),
                html.Div([
                    html.Label("Composite value (optional)", style=_label_style),
                    dcc.Dropdown(id="sb-rule-composite", options=[
                        {"label": "None (random string only)", "value": "none"},
                        {"label": "UUID (prefix + UUID)", "value": "prefix_uuid"},
                        {"label": "UUID + suffix", "value": "uuid_suffix"},
                        {"label": "Prefix + UUID + suffix", "value": "prefix_uuid_suffix"},
                        {"label": "Prefix + random string", "value": "prefix_random"},
                        {"label": "Random string + suffix", "value": "random_suffix"},
                    ], value="none", clearable=False, style={"fontSize": "12px", "marginTop": "3px"}),
                ], style={"marginTop": "6px"}),
            ])
        if field_type in ("int", "float", "decimal"):
            return html.Div([
                html.Div([
                    html.Div([html.Label("Min value", style=_label_style), dcc.Input(id="sb-rule-min", type="number", value=0, style=_input_style)], style={"flex": "1"}),
                    html.Div([html.Label("Max value", style=_label_style), dcc.Input(id="sb-rule-max", type="number", value=10000, style=_input_style)], style={"flex": "1"}),
                ], style={"display": "flex", "gap": "8px"}),
            ])
        if field_type == "choice":
            return html.Div([
                html.Label("Choices (comma-separated)", style=_label_style),
                dcc.Input(id="sb-rule-choices", type="text", placeholder="new, paid, shipped, cancelled", style=_input_style),
            ])
        if field_type == "faker":
            return html.Div([
                html.Label("Faker provider", style=_label_style),
                dcc.Dropdown(
                    id="sb-rule-faker",
                    options=[{"label": p, "value": p} for p in FAKER_PROVIDERS],
                    value="name",
                    clearable=False,
                    style={"fontSize": "12px", "marginTop": "3px"},
                ),
            ])
        if field_type in ("datetime", "date_range"):
            return html.Div([
                html.Div([
                    html.Div([
                        html.Label("Date mode", style=_label_style),
                        dcc.Dropdown(
                            id="sb-rule-date-mode",
                            options=[{"label": m, "value": m} for m in ["now", "random_past", "random_future", "increment"]],
                            value="now", clearable=False, style={"fontSize": "12px", "marginTop": "3px"},
                        ),
                    ], style={"flex": "1"}),
                    html.Div([html.Label("Range (days)", style=_label_style), dcc.Input(id="sb-rule-date-days", type="number", value=365, min=1, style=_input_style)], style={"flex": "1"}),
                ], style={"display": "flex", "gap": "8px"}),
            ])
        if field_type == "array":
            return html.Div([
                html.Div([
                    html.Div([html.Label("Element type", style=_label_style),
                        dcc.Dropdown(id="sb-rule-array-type", options=[{"label": t, "value": t} for t in FIELD_TYPES if t != "array"], value="string", clearable=False, style={"fontSize": "12px", "marginTop": "3px"})
                    ], style={"flex": "1"}),
                    html.Div([html.Label("Min items", style=_label_style), dcc.Input(id="sb-rule-min-items", type="number", value=1, min=0, style=_input_style)], style={"flex": "0.5"}),
                    html.Div([html.Label("Max items", style=_label_style), dcc.Input(id="sb-rule-max-items", type="number", value=5, min=1, style=_input_style)], style={"flex": "0.5"}),
                ], style={"display": "flex", "gap": "8px"}),
            ])
        if field_type == "object":
            return html.Div(
                "Embedded document: add the parent field, then add sub-fields with dot-notation names (e.g. 'address.street', 'address.city').",
                style={"fontSize": "11px", "color": "#6b7280", "fontStyle": "italic", "padding": "6px 0"},
            )
        return html.Div()

    @callback(
        Output("schema-builder-fields", "data", allow_duplicate=True),
        Output("sb-add-feedback", "children"),
        Output("sb-field-name", "value"),
        Input("sb-add-field-btn", "n_clicks"),
        State("sb-field-name", "value"),
        State("sb-field-type", "value"),
        State("schema-builder-fields", "data"),
        State("sb-rule-inputs", "children"),
        prevent_initial_call=True,
    )
    def add_schema_field(n_clicks, field_name, field_type, current_fields, rule_children):
        if not n_clicks:
            raise PreventUpdate
        name = (field_name or "").strip().strip("'\"")
        if not name:
            return no_update, _feedback_warning("Field name is required."), no_update
        if "'" in name or '"' in name:
            return no_update, _feedback_warning("Field names must not contain quotes. Enter the name without quotes (e.g. tenant_id, not 'tenant_id')."), no_update
        if " " in name:
            return no_update, _feedback_warning("Field names must not contain spaces. Use underscores (e.g. order_total)."), no_update
        if not all(c.isalnum() or c in ("_", ".", "-") for c in name):
            return no_update, _feedback_warning("Field names may only contain letters, digits, underscores, hyphens, and dots (for nesting)."), no_update
        fields = current_fields or []
        all_names = {f["name"] for f in fields}
        if name in all_names:
            return no_update, _feedback_warning(f"Field '{name}' already exists."), no_update
        field: dict = {"name": name, "field_type": field_type}
        _extract_rules_from_children(field, field_type, rule_children)
        fields.append(field)
        return fields, _feedback_info(f"Added '{name}' ({field_type})"), ""

    @callback(
        Output("sb-field-list", "children"),
        Output("sb-field-count", "children"),
        Input("schema-builder-fields", "data"),
    )
    def render_field_list(fields):
        fields = fields or []
        if not fields:
            return html.Div("No fields added yet. Use the form above to add fields.", style={"fontSize": "12px", "color": "#9ca3af", "padding": "12px 0"}), "0 fields"
        rows = []
        for i, f in enumerate(fields):
            ft = f.get("field_type", "string")
            detail_parts = []
            if ft == "string":
                parts = []
                if f.get("prefix"):
                    parts.append(f'"{f["prefix"]}"')
                comp = f.get("composite", "none")
                if comp and comp != "none" and "uuid" in comp:
                    parts.append("UUID")
                elif f.get("length"):
                    parts.append(f"rand({f['length']})")
                else:
                    parts.append(f"rand({f.get('length', 16)})")
                if f.get("suffix"):
                    parts.append(f'"{f["suffix"]}"')
                detail_parts.append("+".join(parts) if len(parts) > 1 else parts[0] if parts else f"len={f.get('length', 16)}")
            elif ft in ("int", "float", "decimal"):
                detail_parts.append(f"{f.get('min_value', 0)}..{f.get('max_value', 10000)}")
            elif ft == "choice":
                choices = f.get("choices", [])
                detail_parts.append(f"[{', '.join(str(c) for c in choices[:3])}{'...' if len(choices)>3 else ''}]")
            elif ft == "faker":
                detail_parts.append(f.get("faker_provider", "name"))
            elif ft in ("datetime", "date_range"):
                detail_parts.append(f.get("date_mode", "now"))
            elif ft == "array":
                detail_parts.append(f"items={f.get('_array_item_type', 'string')}, {f.get('min_items', 1)}-{f.get('max_items', 5)}")
            detail = " ".join(detail_parts) if detail_parts else ""
            rows.append(html.Div(
                [
                    html.Div([
                        html.Span(f["name"], style={"fontWeight": "600", "color": "#1e1b4b", "fontSize": "13px"}),
                        html.Span(f" {ft}", style={"color": "#6366f1", "fontSize": "11px", "marginLeft": "6px"}),
                        html.Span(f" {detail}", style={"color": "#9ca3af", "fontSize": "11px", "marginLeft": "4px"}) if detail else None,
                    ], style={"display": "flex", "alignItems": "center", "flex": "1"}),
                    html.Button(
                        "Remove", id={"type": "sb-remove-field", "index": i}, n_clicks=0,
                        style={"background": "none", "border": "1px solid #fca5a5", "color": "#dc2626", "borderRadius": "6px", "padding": "2px 10px", "fontSize": "11px", "cursor": "pointer", "fontWeight": "600"},
                    ),
                ],
                style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "padding": "6px 10px", "borderRadius": "8px", "background": "#f9fafb" if i % 2 == 0 else "white", "marginBottom": "2px"},
            ))
        return rows, f"{len(fields)} field(s)"

    @callback(
        Output("schema-builder-fields", "data", allow_duplicate=True),
        Input({"type": "sb-remove-field", "index": dash.ALL}, "n_clicks"),
        State("schema-builder-fields", "data"),
        prevent_initial_call=True,
    )
    def remove_schema_field(all_clicks, current_fields):
        if not any(c and c > 0 for c in (all_clicks or [])):
            raise PreventUpdate
        triggered = ctx.triggered_id
        if triggered is None or not isinstance(triggered, dict):
            raise PreventUpdate
        idx = triggered.get("index")
        fields = list(current_fields or [])
        if idx is not None and 0 <= idx < len(fields):
            fields.pop(idx)
        return fields

    @callback(
        Output("schema-fields-json-input", "value", allow_duplicate=True),
        Output("schema-builder-overlay", "style", allow_duplicate=True),
        Output("schema-validation-feedback", "children", allow_duplicate=True),
        Input("sb-save-btn", "n_clicks"),
        State("schema-builder-fields", "data"),
        State("sb-target-selector", "value"),
        prevent_initial_call=True,
    )
    def save_schema_from_builder(n_clicks, fields, target_value):
        if not n_clicks:
            raise PreventUpdate
        fields = fields or []
        if not fields:
            return no_update, no_update, _feedback_warning("No fields to save. Add at least one field.")
        schema_fields = _builder_fields_to_schema(fields)
        payload = {"fields": schema_fields}
        json_str = json.dumps(payload, indent=2, sort_keys=False)
        try:
            schema = _parse_schema_config(json_str)
        except ValueError as exc:
            return no_update, no_update, _feedback_error(f"Generated schema is invalid: {exc}")
        target = target_value or "__all__"
        if target != "__all__":
            parts = target.split(".", 1)
            if len(parts) == 2:
                db_name, coll_name = parts
                runner.register_collection_schema(db_name, coll_name, schema)
                if metadata_store is not None:
                    metadata_store.save_collection_schema(
                        db_name, coll_name, schema.model_dump(mode="json"),
                    )
                    metadata_store.append_event(
                        event_type="collection_schema_saved",
                        message=f"Per-collection schema saved for {target}.",
                        payload={"target": target, "field_count": len(schema.fields)},
                    )
        hidden_style = {"display": "none"}
        if target != "__all__":
            msg = f"Schema with {len(schema_fields)} field(s) saved for {target}."
        else:
            msg = f"Default schema applied with {len(schema_fields)} field(s)."
        return json_str, hidden_style, _feedback_info(msg)

    @callback(
        Output("schema-builder-fields", "data", allow_duplicate=True),
        Output("sb-add-feedback", "children", allow_duplicate=True),
        Input("sb-load-current-btn", "n_clicks"),
        State("schema-fields-json-input", "value"),
        prevent_initial_call=True,
    )
    def load_current_into_builder(n_clicks, current_json):
        if not n_clicks:
            raise PreventUpdate
        try:
            schema = _parse_schema_config(current_json)
        except ValueError as exc:
            return no_update, _feedback_error(f"Cannot load: {exc}")
        fields = _schema_to_builder_fields(schema.fields)
        return fields, _feedback_info(f"Loaded {len(fields)} field(s) from current schema.")

    @callback(
        Output("schema-builder-fields", "data", allow_duplicate=True),
        Output("sb-add-feedback", "children", allow_duplicate=True),
        Output("sb-target-info", "children", allow_duplicate=True),
        Input("sb-target-selector", "value"),
        prevent_initial_call=True,
    )
    def on_target_changed(target_value):
        target = target_value or "__all__"
        if target == "__all__":
            try:
                default_schema = controller.current_config().document_schema
                fields = _schema_to_builder_fields(default_schema.fields)
                return (
                    fields,
                    _feedback_info(f"Loaded default schema ({len(fields)} field(s)). Modify and save to update the default."),
                    "Editing the default schema applied to all collections without a custom override.",
                )
            except Exception:
                return [], "", "Default schema selected."
        parts = target.split(".", 1)
        if len(parts) != 2:
            return no_update, no_update, no_update
        db_name, coll_name = parts
        if runner.schema_registry.has_override(db_name, coll_name):
            schema = runner.schema_registry.get_schema(db_name, coll_name)
            fields = _schema_to_builder_fields(schema.fields)
            info = html.Div([
                html.Span("CUSTOM", style={
                    "fontWeight": "800", "fontSize": "10px", "letterSpacing": "0.05em",
                    "marginRight": "6px", "background": "#4f46e5", "color": "white",
                    "padding": "1px 6px", "borderRadius": "4px",
                }),
                f" {target} has a custom schema with {len(fields)} field(s). Modify and save to update it.",
            ])
            return (
                fields,
                _feedback_info(f"Loaded custom schema for {target} ({len(fields)} field(s))."),
                info,
            )
        default_schema = controller.current_config().document_schema
        fields = _schema_to_builder_fields(default_schema.fields)
        info = html.Div([
            html.Span("DEFAULT", style={
                "fontWeight": "800", "fontSize": "10px", "letterSpacing": "0.05em",
                "marginRight": "6px", "background": "#059669", "color": "white",
                "padding": "1px 6px", "borderRadius": "4px",
            }),
            f" {target} uses the default schema. Add fields and save to create a custom schema for this collection.",
        ])
        return (
            fields,
            _feedback_info(f"Loaded default schema for {target} ({len(fields)} field(s)). Customize and save to override."),
            info,
        )

    @callback(
        Output("duration-seconds-input", "value"),
        Output("target-users-input", "value"),
        Output("worker-threads-input", "value"),
        Output("think-time-min-input", "value"),
        Output("think-time-max-input", "value"),
        Output("target-ops-per-user-input", "value"),
        Output("mix-read-input", "value"),
        Output("mix-insert-input", "value"),
        Output("mix-update-input", "value"),
        Output("mix-delete-input", "value"),
        Output("mix-fam-input", "value"),
        Output("retry-max-attempts-input", "value"),
        Output("retry-initial-backoff-input", "value"),
        Output("retry-max-backoff-input", "value"),
        Output("retry-multiplier-input", "value"),
        Output("retry-jitter-input", "value"),
        Output("runtime-log-level-input", "value"),
        Output("progress-log-interval-input", "value"),
        Output("log-operation-failures-toggle", "value"),
        Output("namespace-db-prefix-input", "value"),
        Output("namespace-collection-prefix-input", "value"),
        Output("namespace-databases-input", "value"),
        Output("namespace-collections-input", "value"),
        Output("initial-seed-input", "value"),
        Output("ramp-up-seconds-input", "value"),
        Output("schema-fields-json-input", "value"),
        Output("control-feedback", "children", allow_duplicate=True),
        Output("static-config-feedback", "children", allow_duplicate=True),
        Input("reset-defaults-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def reset_all_settings_to_defaults(reset_clicks: int):
        if not reset_clicks:
            raise PreventUpdate
        try:
            defaults = default_config()
            if runner.is_running:
                # Live-safe controls apply now; static controls are staged for next run.
                controller.update_duration_seconds(defaults.load.duration_seconds)
                controller.update_target_virtual_users(defaults.load.virtual_users)
                controller.update_worker_threads(defaults.load.worker_threads)
                controller.update_pacing(
                    think_time_ms_min=defaults.load.think_time_ms_min,
                    think_time_ms_max=defaults.load.think_time_ms_max,
                    target_ops_per_user_per_sec=defaults.load.target_ops_per_user_per_sec,
                )
                controller.update_operation_mix(
                    read=defaults.operation_mix.read,
                    insert=defaults.operation_mix.insert,
                    update=defaults.operation_mix.update,
                    delete=defaults.operation_mix.delete,
                    find_and_modify=defaults.operation_mix.find_and_modify,
                )
                controller.update_retry_policy(
                    max_attempts=defaults.retry.max_attempts,
                    initial_backoff_ms=defaults.retry.initial_backoff_ms,
                    max_backoff_ms=defaults.retry.max_backoff_ms,
                    backoff_multiplier=defaults.retry.backoff_multiplier,
                    jitter_ratio=defaults.retry.jitter_ratio,
                )
                controller.update_runtime_logging(
                    level_name=defaults.logging.level,
                    progress_log_interval_seconds=defaults.logging.progress_log_interval_seconds,
                    log_operation_failures=defaults.logging.log_operation_failures,
                )
                staged_config = controller.current_config()
                staged_config.namespace = defaults.namespace.model_copy(deep=True)
                staged_config.document_schema = defaults.document_schema.model_copy(deep=True)
                staged_config.load.initial_seed_per_collection = defaults.load.initial_seed_per_collection
                staged_config.load.ramp_up_seconds = defaults.load.ramp_up_seconds
                run_state["staged_static_snapshot"] = _static_config_snapshot(staged_config)
                persist_metadata_config(
                    "reset-all-defaults-running",
                    config_override=staged_config,
                    mark_synced=False,
                )
                message = (
                    "Live defaults applied now; static defaults are staged for next run and metadata updated."
                )
                static_message = (
                    "Defaults staged for next run while load test is active (namespace/schema/seed/ramp-up)."
                )
            else:
                config_for_reset = controller.current_config()
                config_for_reset.load.duration_seconds = defaults.load.duration_seconds
                config_for_reset.load.virtual_users = defaults.load.virtual_users
                config_for_reset.load.worker_threads = defaults.load.worker_threads
                config_for_reset.load.think_time_ms_min = defaults.load.think_time_ms_min
                config_for_reset.load.think_time_ms_max = defaults.load.think_time_ms_max
                config_for_reset.load.target_ops_per_user_per_sec = defaults.load.target_ops_per_user_per_sec
                config_for_reset.load.initial_seed_per_collection = defaults.load.initial_seed_per_collection
                config_for_reset.load.ramp_up_seconds = defaults.load.ramp_up_seconds
                config_for_reset.operation_mix = defaults.operation_mix.model_copy(deep=True)
                config_for_reset.retry = defaults.retry.model_copy(deep=True)
                config_for_reset.logging.level = defaults.logging.level
                config_for_reset.logging.progress_log_interval_seconds = defaults.logging.progress_log_interval_seconds
                config_for_reset.logging.log_operation_failures = defaults.logging.log_operation_failures
                config_for_reset.namespace = defaults.namespace.model_copy(deep=True)
                config_for_reset.document_schema = defaults.document_schema.model_copy(deep=True)
                controller.apply_config(config_for_reset, source="dashboard-reset-defaults")
                run_state["staged_static_snapshot"] = None
                persist_metadata_config("reset-all-defaults")
                message = "All configurable settings were reset to defaults and metadata has been updated."
                static_message = (
                    "Defaults restored. Static namespace/schema/seed/ramp-up values are staged "
                    "according to current run state."
                )
            schema_json = _schema_config_to_json(defaults.document_schema)
            return (
                defaults.load.duration_seconds,
                str(defaults.load.virtual_users),
                str(defaults.load.worker_threads),
                defaults.load.think_time_ms_min,
                defaults.load.think_time_ms_max,
                (
                    ""
                    if defaults.load.target_ops_per_user_per_sec is None
                    else f"{defaults.load.target_ops_per_user_per_sec}"
                ),
                defaults.operation_mix.read,
                defaults.operation_mix.insert,
                defaults.operation_mix.update,
                defaults.operation_mix.delete,
                defaults.operation_mix.find_and_modify,
                defaults.retry.max_attempts,
                defaults.retry.initial_backoff_ms,
                defaults.retry.max_backoff_ms,
                defaults.retry.backoff_multiplier,
                defaults.retry.jitter_ratio,
                defaults.logging.level,
                defaults.logging.progress_log_interval_seconds,
                ["enabled"] if defaults.logging.log_operation_failures else [],
                defaults.namespace.database_prefix,
                defaults.namespace.collection_prefix,
                defaults.namespace.databases,
                defaults.namespace.collections_per_database,
                defaults.load.initial_seed_per_collection,
                defaults.load.ramp_up_seconds,
                schema_json,
                _feedback_info(message),
                static_message,
            )
        except Exception as exc:  # pragma: no cover - UI safety
            logger.exception("Reset defaults callback failed.")
            message = f"Reset defaults failed: {exc}"
            return (
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                _feedback_error(message),
                message,
            )

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
            html.Div(label_node, style={"fontSize": "11px", "color": "#6b7280", "fontWeight": "500", "textTransform": "uppercase", "letterSpacing": "0.04em"}),
            html.Div(id=value_id, style={"fontSize": "24px", "fontWeight": "800", "marginTop": "6px", "color": "#111827", "letterSpacing": "-0.02em"}),
        ],
        style={
            "background": background,
            "border": f"1px solid {border}",
            "borderRadius": "12px",
            "padding": "14px 16px",
            "boxShadow": "0 1px 2px rgba(0,0,0,0.04)",
            "minHeight": "78px",
        },
    )


_PRE_STYLE = {
    "fontSize": "10.5px", "background": "#f1f5f9", "padding": "8px",
    "borderRadius": "6px", "marginTop": "4px", "border": "1px solid #e2e8f0",
    "lineHeight": "1.5", "whiteSpace": "pre-wrap",
}


def _schema_ref_table(rows: list[tuple[str, str, str]]) -> html.Table:
    _td = {"padding": "2px 8px"}
    _mono = {"padding": "2px 8px", "fontFamily": "monospace", "fontSize": "10px"}
    return html.Table(
        [
            html.Thead(html.Tr([
                html.Th("Type", style={"textAlign": "left", "padding": "3px 8px", "fontSize": "11px"}),
                html.Th("Properties", style={"textAlign": "left", "padding": "3px 8px", "fontSize": "11px"}),
                html.Th("Example", style={"textAlign": "left", "padding": "3px 8px", "fontSize": "11px"}),
            ])),
            html.Tbody([
                html.Tr([html.Td(t, style=_td), html.Td(p, style=_td), html.Td(e, style=_mono)])
                for t, p, e in rows
            ]),
        ],
        style={"width": "100%", "borderCollapse": "collapse", "fontSize": "11px", "marginTop": "4px"},
    )


def _chart_title(label: str, tooltip: str) -> html.Div:
    return html.Div(
        [
            html.Span(label, style={"fontWeight": "600", "color": "#312e81"}),
            html.Span(
                "?",
                title=tooltip,
                style={
                    "fontSize": "10px",
                    "padding": "0px 5px",
                    "borderRadius": "999px",
                    "border": "1px solid #a5b4fc",
                    "color": "#4338ca",
                    "fontWeight": "700",
                    "cursor": "help",
                    "lineHeight": "16px",
                    "display": "inline-block",
                    "textAlign": "center",
                },
            ),
        ],
        style={"display": "flex", "gap": "8px", "alignItems": "center", "marginBottom": "6px"},
    )


def _label_with_help(label: str, tooltip: str, color: str = "#4b5563", margin_top: str = "0px") -> html.Div:
    return html.Div(
        [
            html.Span(label),
            html.Span(
                "i",
                title=tooltip,
                style={
                    "fontSize": "10px",
                    "padding": "1px 5px",
                    "borderRadius": "999px",
                    "border": "1px solid #a5b4fc",
                    "color": "#4338ca",
                    "fontWeight": "700",
                    "cursor": "help",
                },
            ),
        ],
        style={"fontSize": "12px", "color": color, "display": "flex", "gap": "6px", "alignItems": "center", "marginTop": margin_top},
    )


def _scope_badge(scope: str, value_id: str | None = None) -> html.Span:
    style = _scope_badge_style("next_run" if scope.lower().startswith("next") else "active_default")
    kwargs = {"id": value_id} if value_id else {}
    return html.Span(
        scope.upper(),
        style=style,
        **kwargs,
    )


def _scope_badge_style(scope_type: str) -> dict[str, str]:
    base = {
        "fontSize": "10px",
        "padding": "2px 6px",
        "borderRadius": "999px",
        "fontWeight": "700",
    }
    if scope_type == "next_run":
        return {**base, "background": "#ede9fe", "color": "#5b21b6", "border": "1px solid #c4b5fd"}
    if scope_type == "user_config":
        return {**base, "background": "#e0f2fe", "color": "#0c4a6e", "border": "1px solid #7dd3fc"}
    return {**base, "background": "#dcfce7", "color": "#166534", "border": "1px solid #86efac"}


def _profile_chip(label: str, value: str, value_id: str | None = None) -> html.Div:
    value_kwargs = {"id": value_id} if value_id else {}
    return html.Div(
        [
            html.Div(label, style={"fontSize": "11px", "color": "#6b7280"}),
            html.Div(
                value,
                style={"fontSize": "14px", "fontWeight": "600", "marginTop": "6px", "color": "#111827"},
                **value_kwargs,
            ),
        ],
        style={**CARD_STYLE, "background": "#faf5ff", "border": "1px solid #d8b4fe"},
    )


def _weighted_input(label: str, input_id: str, value: float, tooltip: str | None = None) -> html.Div:
    return html.Div(
        [
            _label_with_help(label, tooltip or label),
            dcc.Input(
                id=input_id,
                type="number",
                min=0,
                step=0.01,
                value=value,
                style={
                    "width": "100%",
                    "padding": "8px",
                    "borderRadius": "8px",
                    "border": "1px solid #d1d5db",
                    "marginTop": "6px",
                },
            ),
        ]
    )


def _compact_number_input(
    label: str,
    input_id: str,
    value: int | float,
    step: int | float = 1,
    min_value: int | float | None = None,
    max_value: int | float | None = None,
    tooltip: str | None = None,
) -> html.Div:
    kwargs: dict[str, int | float] = {"step": step}
    if min_value is not None:
        kwargs["min"] = min_value
    if max_value is not None:
        kwargs["max"] = max_value
    return html.Div(
        [
            _label_with_help(label, tooltip or label),
            dcc.Input(
                id=input_id,
                type="number",
                value=value,
                style={
                    "width": "100%",
                    "padding": "8px",
                    "borderRadius": "8px",
                    "border": "1px solid #d1d5db",
                    "marginTop": "6px",
                },
                **kwargs,
            ),
        ]
    )


def _build_operation_table(snapshot: MetricsSnapshot) -> html.Table:
    operations = ("read", "insert", "update", "delete", "find_and_modify")
    total_ops = snapshot.total_ops
    cell_style = {"padding": "8px", "borderBottom": "1px solid #ddd6fe"}
    rows: list[html.Tr] = []
    for op_name in operations:
        count = snapshot.operation_counts.get(op_name, 0)
        share = (count / total_ops * 100.0) if total_ops else 0.0
        avg_latency = snapshot.operation_latency_avg_ms.get(op_name, 0.0)
        p95 = snapshot.operation_latency_p95_ms.get(op_name, 0.0)
        p99 = snapshot.operation_latency_p99_ms.get(op_name, 0.0)
        rows.append(
            html.Tr(
                [
                    html.Td(op_name, style=cell_style),
                    html.Td(f"{count:,}", style=cell_style),
                    html.Td(f"{share:.2f}%", style=cell_style),
                    html.Td(f"{avg_latency:.2f} ms", style=cell_style),
                    html.Td(f"{p95:.2f} ms", style=cell_style),
                    html.Td(f"{p99:.2f} ms", style=cell_style),
                ]
            )
        )
    header_style = {"textAlign": "left", "padding": "8px"}
    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Operation", style=header_style),
                        html.Th("Count", style=header_style),
                        html.Th("Share", style=header_style),
                        html.Th("Avg latency", style=header_style),
                        html.Th("P95 latency", style=header_style),
                        html.Th("P99 latency", style=header_style),
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
    return "READY (not started)", STATUS_STYLE_MAP["initializing"]


def _build_runtime_ui(runner: LoadTestRunner) -> tuple[str, float, str]:
    started_at = runner.started_at
    finished_at = runner.finished_at
    target_duration = max(runner.target_duration_seconds, 1)
    active_elapsed = runner.active_elapsed_seconds()
    if not started_at:
        return "Waiting for user to start run", 0.0, "0.0% complete"
    if finished_at:
        elapsed = max((finished_at - started_at).total_seconds(), 0.0)
        return f"Completed in {elapsed:.1f}s", 100.0, "100.0% complete"
    progress = min((active_elapsed / target_duration) * 100.0, 100.0)
    remaining = max(target_duration - active_elapsed, 0.0)
    return f"Elapsed: {active_elapsed:.1f}s | Remaining: {remaining:.1f}s", progress, f"{progress:.1f}% complete"


def _filter_snapshot_timeline(
    snapshot: MetricsSnapshot,
    window_seconds: int,
    reference_time: datetime | None = None,
) -> MetricsSnapshot:
    if window_seconds <= 0:
        return snapshot
    ref = reference_time or snapshot.generated_at
    cutoff = ref - timedelta(seconds=window_seconds)
    filtered = [point for point in snapshot.timeline if point.second >= cutoff]
    return replace(snapshot, timeline=filtered)


def _filter_snapshot_to_run_start(snapshot: MetricsSnapshot, run_started_at: datetime | None) -> MetricsSnapshot:
    if run_started_at is None:
        return snapshot
    run_start = run_started_at.replace(microsecond=0)
    filtered = [point for point in snapshot.timeline if point.second >= run_start]
    return replace(snapshot, timeline=filtered)


def _timeline_x_range(
    window_seconds: int,
    generated_at: datetime,
    run_started_at: datetime | None,
    is_running: bool = False,
    finished_at: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    if run_started_at is None:
        return None
    if is_running:
        end = generated_at
    elif finished_at is not None:
        end = finished_at
    else:
        end = generated_at
    if window_seconds > 0:
        start = end - timedelta(seconds=window_seconds)
        return start, end
    return run_started_at.replace(microsecond=0), end


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
    message = f"Showing last {_format_duration(window_seconds)} window (charts keep full selected span)."
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


def _schema_config_to_json(schema: SchemaConfig) -> str:
    payload = {
        "fields": [field.model_dump(mode="json", exclude_defaults=True) for field in schema.fields],
    }
    if schema.query_targets.enabled:
        payload["query_targets"] = schema.query_targets.model_dump(mode="json", exclude_defaults=True)
    return json.dumps(payload, indent=2, sort_keys=False)


def _static_config_snapshot(config: AppConfig) -> dict[str, object]:
    return {
        "namespace.database_prefix": config.namespace.database_prefix,
        "namespace.collection_prefix": config.namespace.collection_prefix,
        "namespace.databases": config.namespace.databases,
        "namespace.collections_per_database": config.namespace.collections_per_database,
        "load.initial_seed_per_collection": config.load.initial_seed_per_collection,
        "load.ramp_up_seconds": config.load.ramp_up_seconds,
        "schema.fields": json.dumps(
            [field.model_dump(mode="json") for field in config.document_schema.fields],
            sort_keys=True,
        ),
    }


def _static_config_diff_labels(current: dict[str, object], baseline: object) -> list[str]:
    if not isinstance(baseline, dict):
        return []
    labels = {
        "namespace.database_prefix": "database prefix",
        "namespace.collection_prefix": "collection prefix",
        "namespace.databases": "databases",
        "namespace.collections_per_database": "collections/database",
        "load.initial_seed_per_collection": "initial seed/collection",
        "load.ramp_up_seconds": "ramp-up seconds",
        "schema.fields": "schema fields",
    }
    changed: list[str] = []
    for key, label in labels.items():
        if current.get(key) != baseline.get(key):
            changed.append(label)
    return changed


def _fmt(value: int) -> str:
    return f"{value:,}"


def _parse_non_empty_text(value: object, field_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} is required.")
    return raw


def _parse_non_negative_int(value: object, field_name: str) -> int:
    raw = str(value or "").strip().replace(",", "")
    if not raw:
        raise ValueError(f"{field_name} value is required.")
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a whole number.") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be zero or greater.")
    return parsed


def _parse_schema_config(value: object) -> SchemaConfig:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Schema fields JSON is required.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Schema fields JSON is invalid. Please provide valid JSON.") from exc
    if isinstance(payload, list):
        schema_payload = {"fields": payload}
    elif isinstance(payload, dict):
        if "fields" not in payload:
            raise ValueError('Schema JSON object must include a "fields" key.')
        schema_payload = payload
    else:
        raise ValueError('Schema JSON must be an array or an object with a "fields" key.')
    try:
        return SchemaConfig.model_validate(schema_payload)
    except Exception as exc:
        raise ValueError(f"Schema validation failed: {exc}") from exc


def _validate_think_time_values(min_value: object, max_value: object) -> tuple[int, int]:
    if min_value is None or max_value is None:
        raise ValueError("Think time minimum and maximum values are required.")
    think_min = int(min_value)
    think_max = int(max_value)
    if think_min > think_max:
        raise ValueError(
            "Warning: Think time minimum cannot be greater than think time maximum. "
            "Please set min <= max and apply again."
        )
    if think_min < 0 or think_max < 0:
        raise ValueError("Think time values must be zero or greater.")
    return think_min, think_max


def _feedback_warning(message: str) -> html.Span:
    return html.Span(
        message,
        style={
            "display": "inline-block",
            "padding": "4px 8px",
            "borderRadius": "8px",
            "background": "#fef3c7",
            "border": "1px solid #f59e0b",
            "color": "#92400e",
            "fontWeight": "600",
        },
    )


def _feedback_info(message: str) -> html.Span:
    return html.Span(
        message,
        style={
            "display": "inline-block",
            "padding": "4px 8px",
            "borderRadius": "8px",
            "background": "#e0f2fe",
            "border": "1px solid #38bdf8",
            "color": "#0c4a6e",
            "fontWeight": "600",
        },
    )


def _feedback_error(message: str) -> html.Span:
    return html.Span(
        message,
        style={
            "display": "inline-block",
            "padding": "4px 8px",
            "borderRadius": "8px",
            "background": "#fee2e2",
            "border": "1px solid #ef4444",
            "color": "#991b1b",
            "fontWeight": "600",
        },
    )


def _parse_positive_int(value: object, field_name: str) -> int:
    raw = str(value or "").strip().replace(",", "")
    if not raw:
        raise ValueError(f"{field_name} value is required.")
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a whole number.") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than 0.")
    return parsed


def _parse_optional_positive_float(value: object, field_name: str) -> float | None:
    raw = str(value or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid number.") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be a positive number.")
    if parsed == 0:
        return None
    return parsed


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
    return (
        f"FAILED: dashboard refresh error ({error_message})",
        style,
        no_update,                      # runtime-meta
        datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        no_update,                      # test-progress
        no_update,                      # test-progress-label
        no_update,                      # window-description
        no_update,                      # duration-config-meta
        no_update,                      # total-ops
        no_update,                      # ops-per-sec
        no_update,                      # error-rate
        no_update,                      # success-rate
        no_update,                      # p50-latency
        no_update,                      # avg-latency
        no_update,                      # p95-latency
        no_update,                      # p99-latency
        no_update,                      # retry-rate
        no_update,                      # active-users-live
        no_update,                      # ops-graph
        no_update,                      # latency-graph
        no_update,                      # users-graph
        no_update,                      # error-retry-graph
        no_update,                      # per-op-throughput-graph
        no_update,                      # per-op-latency-graph
        no_update,                      # distribution-graph
        no_update,                      # error-breakdown-graph
        no_update,                      # latency-histogram-graph
        no_update,                      # operation-table
        no_update,                      # smart-insights
        True,                           # stop-test-btn disabled
        True,                           # pause-test-btn disabled
        True,                           # resume-test-btn disabled
        f"Dashboard refresh failed: {error_message}. Check application logs.",
        no_update,                      # log-meta
    )


def _extract_rules_from_children(field: dict, field_type: str, rule_children: object) -> None:
    """Best-effort extraction of rule values from the rendered rule inputs."""
    if rule_children is None:
        return
    props = _flatten_component_props(rule_children)
    if field_type == "string":
        field["length"] = int(props.get("sb-rule-length", 16) or 16)
        prefix = str(props.get("sb-rule-prefix", "") or "")
        suffix = str(props.get("sb-rule-suffix", "") or "")
        composite = str(props.get("sb-rule-composite", "none") or "none")
        if prefix:
            field["prefix"] = prefix
        if suffix:
            field["suffix"] = suffix
        if composite != "none":
            field["composite"] = composite
    elif field_type in ("int", "float", "decimal"):
        field["min_value"] = float(props.get("sb-rule-min", 0) or 0)
        field["max_value"] = float(props.get("sb-rule-max", 10000) or 10000)
    elif field_type == "choice":
        raw = str(props.get("sb-rule-choices", "") or "")
        field["choices"] = [c.strip() for c in raw.split(",") if c.strip()]
    elif field_type == "faker":
        field["faker_provider"] = str(props.get("sb-rule-faker", "name") or "name")
    elif field_type in ("datetime", "date_range"):
        field["date_mode"] = str(props.get("sb-rule-date-mode", "now") or "now")
        field["date_range_days"] = int(props.get("sb-rule-date-days", 365) or 365)
    elif field_type == "array":
        field["_array_item_type"] = str(props.get("sb-rule-array-type", "string") or "string")
        field["min_items"] = int(props.get("sb-rule-min-items", 1) or 1)
        field["max_items"] = int(props.get("sb-rule-max-items", 5) or 5)


def _flatten_component_props(component: object, result: dict | None = None) -> dict:
    """Walk a Dash component tree and collect id→value pairs."""
    if result is None:
        result = {}
    if hasattr(component, "id") and hasattr(component, "value"):
        result[component.id] = component.value
    if hasattr(component, "children"):
        children = component.children
        if isinstance(children, list):
            for child in children:
                _flatten_component_props(child, result)
        elif children is not None:
            _flatten_component_props(children, result)
    return result


def _builder_fields_to_schema(fields: list[dict]) -> list[dict]:
    """Convert builder field dicts to schema-compatible dicts, resolving dot-notation into nested objects."""
    root_fields: list[dict] = []
    object_children: dict[str, list[dict]] = {}
    for f in fields:
        name = f.get("name", "")
        if "." in name:
            parent, child_name = name.split(".", 1)
            child = dict(f)
            child["name"] = child_name
            object_children.setdefault(parent, []).append(child)
        else:
            root_fields.append(dict(f))
    for parent_name, children in object_children.items():
        existing = next((f for f in root_fields if f["name"] == parent_name), None)
        if existing is not None and existing.get("field_type") == "object":
            existing.setdefault("fields", []).extend(_builder_fields_to_schema(children))
        else:
            root_fields.append({
                "name": parent_name,
                "field_type": "object",
                "fields": _builder_fields_to_schema(children),
            })
    for f in root_fields:
        if f.get("field_type") == "array":
            item_type = f.pop("_array_item_type", "string")
            f["items"] = {"field_type": item_type}
        f.pop("_array_item_type", None)
    return root_fields


def _schema_to_builder_fields(fields: list, prefix: str = "") -> list[dict]:
    """Flatten a nested SchemaConfig field list into builder-style flat dicts."""
    result: list[dict] = []
    for f in fields:
        d = f.model_dump(mode="json", exclude_defaults=True) if hasattr(f, "model_dump") else dict(f)
        name = d.get("name", "")
        full_name = f"{prefix}{name}" if prefix else name
        if d.get("field_type") == "object" and d.get("fields"):
            sub_fields = d.pop("fields")
            d["name"] = full_name
            result.append(d)
            result.extend(_schema_to_builder_fields(sub_fields, prefix=f"{full_name}."))
        elif d.get("field_type") == "array" and d.get("items"):
            items = d.pop("items")
            d["_array_item_type"] = items.get("field_type", "string") if isinstance(items, dict) else "string"
            d["name"] = full_name
            result.append(d)
        else:
            d["name"] = full_name
            result.append(d)
    return result


def _compute_stable_ops_per_sec(total_ops: int, active_elapsed_seconds: float) -> float:
    if total_ops <= 0:
        return 0.0
    if active_elapsed_seconds <= 0:
        return 0.0
    return total_ops / active_elapsed_seconds
