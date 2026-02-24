"""Dash app for live MongoDB load-test telemetry."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, callback, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from plotly.subplots import make_subplots

from mongo_load_tester.config import AppConfig, SchemaConfig, default_config
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
from mongo_load_tester.metadata.store import MetadataStore
from mongo_load_tester.metadata.sync import ConfigSyncService
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
START_BUTTON_STYLE = {
    "padding": "10px 12px",
    "borderRadius": "8px",
    "border": "1px solid #059669",
    "background": "#059669",
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
    metadata_store: MetadataStore | None = None,
    config_sync_service: ConfigSyncService | None = None,
    initial_config_version: int = 1,
) -> Dash:
    app = Dash(__name__, title="MongoDB Load Tester")
    logger = logging.getLogger("mongo_load_tester.dashboard")
    run_state: dict[str, object | None] = {
        "active_run_id": None,
        "last_run_finished_at": None,
        "config_version": int(initial_config_version),
        "run_static_baseline": None,
        "startup_static_baseline": None,
        "staged_static_snapshot": None,
        "selected_run": "__live__",
    }

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
                    "background": "linear-gradient(90deg, #eef2ff 0%, #faf5ff 100%)",
                    "border": "1px solid #c4b5fd",
                    "borderRadius": "12px",
                    "padding": "16px",
                },
            ),
            html.Details(
                [
                    html.Summary(
                        "Operator quick guide",
                        style={"cursor": "pointer", "fontWeight": "600", "color": "#312e81"},
                    ),
                    html.Div(
                        [
                            html.Ul(
                                [
                                    html.Li("Review live controls (apply immediately) and static controls (apply per scope badge)."),
                                    html.Li("Start the run from the UI. Monitor status, runtime progress, and insights."),
                                    html.Li(
                                        "If static settings are changed during an active run, they are staged for next run."
                                    ),
                                    html.Li("Use exports (CSV/PNG/PDF) for reporting and evidence."),
                                ],
                                style={"marginTop": "8px", "marginBottom": "0", "paddingLeft": "18px", "lineHeight": "1.7"},
                            )
                        ],
                        style={"fontSize": "12px", "color": "#4b5563"},
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
                                            "width": "120px",
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
                                ],
                                style={"display": "grid", "gridTemplateColumns": "repeat(2, 1fr)", "gap": "8px", "marginTop": "8px"},
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
                                    html.Span("Static config domains"),
                                    _scope_badge("ready", value_id="static-domain-scope-badge"),
                                ],
                                style={"fontSize": "12px", "color": "#6b7280", "display": "flex", "gap": "8px", "alignItems": "center"},
                            ),
                            html.Div(
                                "These settings define startup defaults used when a run begins.",
                                id="static-domain-scope-note",
                                style={"marginTop": "6px", "fontSize": "12px", "color": "#6b7280"},
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
                            html.Button(
                                "Save static config (next run)",
                                id="apply-static-config-btn",
                                n_clicks=0,
                                style={**ACTION_BUTTON_STYLE, "marginTop": "10px"},
                            ),
                            html.Div(
                                id="static-config-feedback",
                                children="No pending static changes.",
                                style={"marginTop": "8px", "fontSize": "12px", "color": "#6b7280"},
                            ),
                            html.Div(
                                id="static-staged-indicator",
                                children="No staged static changes pending next run.",
                                style={
                                    "marginTop": "8px",
                                    "fontSize": "12px",
                                    "padding": "6px 8px",
                                    "borderRadius": "8px",
                                    "background": "#ecfdf5",
                                    "border": "1px solid #86efac",
                                    "color": "#166534",
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
                                    html.Span("Schema fields JSON"),
                                    _scope_badge("ready", value_id="schema-domain-scope-badge"),
                                ],
                                style={"fontSize": "12px", "color": "#4b5563", "display": "flex", "gap": "8px", "alignItems": "center"},
                            ),
                            dcc.Textarea(
                                id="schema-fields-json-input",
                                value=schema_fields_json_default,
                                style={
                                    "marginTop": "8px",
                                    "width": "100%",
                                    "height": "188px",
                                    "padding": "10px",
                                    "borderRadius": "8px",
                                    "border": "1px solid #d1d5db",
                                    "fontFamily": "Menlo, Consolas, monospace",
                                    "fontSize": "12px",
                                },
                            ),
                            html.Div(
                                "Use either a fields array or {\"fields\": [...]} payload.",
                                id="schema-domain-scope-note",
                                style={"marginTop": "8px", "fontSize": "12px", "color": "#6b7280"},
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
                            html.Div("Users Ramp View", style={"fontWeight": "600", "marginBottom": "6px", "color": "#312e81"}),
                            dcc.Graph(id="users-graph", style={"height": "350px"}),
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
                            html.Div("Latency Health", style={"fontWeight": "600", "marginBottom": "6px", "color": "#312e81"}),
                            dcc.Graph(id="latency-graph", style={"height": "340px"}),
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
                            html.Div("Operation Mix", style={"fontWeight": "600", "marginBottom": "6px", "color": "#312e81"}),
                            dcc.Graph(id="distribution-graph", style={"height": "330px"}),
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
            persist_run_completion_if_needed(snapshot)
            snapshot = _filter_snapshot_to_run_start(snapshot, runner.started_at)
            window_seconds = int(timeline_window or 0)
            filtered_snapshot = _filter_snapshot_timeline(snapshot, window_seconds)
            filtered_snapshot = _remove_incomplete_live_second(filtered_snapshot, runner.is_running)
            x_range = _timeline_x_range(window_seconds, snapshot.generated_at, runner.started_at)
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
                build_operations_timeline(filtered_snapshot, x_range=x_range),
                build_latency_timeline(filtered_snapshot, x_range=x_range),
                build_user_ramp_timeline(filtered_snapshot, x_range=x_range),
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
        Output("static-domain-scope-note", "children"),
        Output("schema-domain-scope-note", "children"),
        Input("refresh", "n_intervals"),
    )
    def refresh_static_scope_state(_: int):
        if runner.is_running:
            label = "NEXT RUN"
            style = _scope_badge_style("next_run")
            return (
                label,
                style,
                label,
                style,
                "Namespace topology, seeding, ramp-up, and schema updates are staged for the next run.",
                "Use either a fields array or {\"fields\": [...]} payload. Saved schema changes apply next run.",
            )
        startup_baseline = run_state.get("startup_static_baseline")
        current_snapshot = _static_config_snapshot(controller.current_config())
        if isinstance(startup_baseline, dict) and current_snapshot != startup_baseline:
            label = "USER CONFIG"
            style = _scope_badge_style("user_config")
            return (
                label,
                style,
                label,
                style,
                "Custom startup configuration is selected and will be used when you click Start.",
                "Custom schema defaults are selected and will be used when the run starts.",
            )
        label = "ACTIVE DEFAULT"
        style = _scope_badge_style("active_default")
        return (
            label,
            style,
            label,
            style,
            "These settings are the active defaults and will be used when you start the load test.",
            "Use either a fields array or {\"fields\": [...]} payload. This schema is used when the run starts.",
        )

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
                return _feedback_info(
                    f"Live load controls applied: users {target_users}/{max_users}, "
                    f"worker threads {worker_threads}{pool_note}, "
                    f"think time {min_ms}-{max_ms} ms, target ops/user {rate_text}."
                )
            if triggered == "apply-operation-mix-btn":
                if not apply_mix_clicks:
                    raise PreventUpdate
                if None in (mix_read_value, mix_insert_value, mix_update_value, mix_delete_value):
                    return _feedback_warning("All operation mix values are required.")
                normalized = controller.update_operation_mix(
                    read=float(mix_read_value),
                    insert=float(mix_insert_value),
                    update=float(mix_update_value),
                    delete=float(mix_delete_value),
                )
                persist_metadata_config("apply-operation-mix")
                return _feedback_info(
                    "Operation mix updated to "
                    f"read {normalized['read'] * 100:.1f}%, "
                    f"insert {normalized['insert'] * 100:.1f}%, "
                    f"update {normalized['update'] * 100:.1f}%, "
                    f"delete {normalized['delete'] * 100:.1f}%."
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


def _filter_snapshot_timeline(snapshot: MetricsSnapshot, window_seconds: int) -> MetricsSnapshot:
    if window_seconds <= 0:
        return snapshot
    cutoff = snapshot.generated_at - timedelta(seconds=window_seconds)
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
) -> tuple[datetime, datetime] | None:
    end = generated_at
    if window_seconds > 0:
        start = generated_at - timedelta(seconds=window_seconds)
        return start, end
    if run_started_at is not None:
        return run_started_at.replace(microsecond=0), end
    return None


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
    payload = [field.model_dump(mode="json") for field in schema.fields]
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
    if parsed <= 0:
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
