"""Figure builders for dashboard and exports."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import plotly.graph_objects as go

from mongo_load_tester.models import MetricsSnapshot

OP_COLORS = {
    "read": "#3b82f6",
    "insert": "#22c55e",
    "update": "#f59e0b",
    "delete": "#ef4444",
    "total": "#111827",
    "users": "#8b5cf6",
}


def build_operations_timeline(snapshot: MetricsSnapshot) -> go.Figure:
    x_values = [point.second for point in snapshot.timeline]
    read_counts = [point.op_counts.get("read", 0) for point in snapshot.timeline]
    insert_counts = [point.op_counts.get("insert", 0) for point in snapshot.timeline]
    update_counts = [point.op_counts.get("update", 0) for point in snapshot.timeline]
    delete_counts = [point.op_counts.get("delete", 0) for point in snapshot.timeline]
    write_counts = [ins + upd + dele for ins, upd, dele in zip(insert_counts, update_counts, delete_counts)]
    total_counts = [point.total_ops for point in snapshot.timeline]

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=total_counts,
            mode="lines",
            name="total_ops",
            line={"width": 3, "color": OP_COLORS["total"]},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=read_counts,
            mode="lines",
            name="read_ops",
            line={"width": 2.5, "color": OP_COLORS["read"]},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=write_counts,
            mode="lines",
            name="write_ops",
            line={"width": 2.5, "color": OP_COLORS["insert"]},
        )
    )
    figure.update_layout(
        title=None,
        xaxis={"title": "", "title_standoff": 0},
        yaxis_title="Operations / sec",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hovermode="x unified",
        margin={"l": 42, "r": 20, "t": 44, "b": 50},
        plot_bgcolor="#f8fafc",
        paper_bgcolor="white",
    )
    figure.update_yaxes(rangemode="tozero")
    _add_pause_overlays(figure, snapshot)
    return figure


def build_latency_timeline(snapshot: MetricsSnapshot) -> go.Figure:
    x_values = [point.second for point in snapshot.timeline]
    avg_values = [point.avg_latency_ms for point in snapshot.timeline]
    p95_values = [point.p95_latency_ms for point in snapshot.timeline]

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=avg_values,
            mode="lines",
            name="avg_latency_ms",
            line={"width": 2.5, "color": "#2563eb"},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=p95_values,
            mode="lines",
            name="p95_latency_ms",
            line={"width": 2.5, "color": "#ec4899"},
        )
    )
    figure.update_layout(
        title="Latency Timeline",
        xaxis={"title": "", "title_standoff": 0},
        yaxis_title="Latency (ms)",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.12, xanchor="left", x=0),
        hovermode="x unified",
        margin={"l": 42, "r": 20, "t": 80, "b": 50},
        plot_bgcolor="#f8fafc",
        paper_bgcolor="white",
    )
    figure.update_yaxes(rangemode="tozero")
    _add_pause_overlays(figure, snapshot)
    return figure


def build_user_ramp_timeline(snapshot: MetricsSnapshot) -> go.Figure:
    x_values = [point.second for point in snapshot.timeline]
    users = [point.active_users for point in snapshot.timeline]
    target_users = [point.target_users for point in snapshot.timeline]
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=users,
            mode="lines",
            name="active_virtual_users",
            line={"width": 2.5, "color": OP_COLORS["users"]},
            fill="tozeroy",
            fillcolor="rgba(139, 92, 246, 0.14)",
        )
    )
    if any(value > 0 for value in target_users):
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=target_users,
                mode="lines",
                name="target_users",
                line={"width": 1.8, "color": "#334155", "dash": "dash"},
            )
        )
    figure.update_layout(
        title=None,
        xaxis={"title": "Time (UTC)", "title_standoff": 16},
        yaxis_title="Active virtual users",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hovermode="x unified",
        margin={"l": 42, "r": 20, "t": 44, "b": 70},
        plot_bgcolor="#f8fafc",
        paper_bgcolor="white",
    )
    figure.update_yaxes(rangemode="tozero")
    _add_pause_overlays(figure, snapshot)
    return figure


def build_operation_distribution(snapshot: MetricsSnapshot) -> go.Figure:
    counts = defaultdict(int, snapshot.operation_counts)
    labels = ["read", "insert", "update", "delete"]
    values = [counts[label] for label in labels]
    total = sum(values)
    if total == 0:
        figure = go.Figure()
        figure.add_annotation(
            text="No operations captured yet",
            x=0.5,
            y=0.5,
            showarrow=False,
            font={"size": 14, "color": "#6b7280"},
        )
        figure.update_layout(
            title="Operation Distribution",
            template="plotly_white",
            xaxis={"visible": False},
            yaxis={"visible": False},
            margin={"l": 20, "r": 20, "t": 50, "b": 20},
            plot_bgcolor="#f8fafc",
            paper_bgcolor="white",
        )
        return figure

    figure = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.45,
                marker={"colors": [OP_COLORS["read"], OP_COLORS["insert"], OP_COLORS["update"], OP_COLORS["delete"]]},
            )
        ]
    )
    figure.update_layout(
        title="Operation Distribution",
        template="plotly_white",
        margin={"l": 20, "r": 20, "t": 50, "b": 20},
        plot_bgcolor="#f8fafc",
        paper_bgcolor="white",
    )
    return figure


def _add_pause_overlays(figure: go.Figure, snapshot: MetricsSnapshot) -> None:
    pause_spans = _pause_spans(snapshot)
    if not pause_spans:
        return
    for start, end in pause_spans:
        figure.add_vrect(
            x0=start,
            x1=end,
            fillcolor="rgba(245, 158, 11, 0.14)",
            line_width=0,
            annotation_text="paused",
            annotation_position="top left",
        )


def _pause_spans(snapshot: MetricsSnapshot) -> list[tuple[object, object]]:
    spans: list[tuple[object, object]] = []
    if not snapshot.timeline:
        return spans
    start = None
    prev = None
    for point in snapshot.timeline:
        if point.paused and start is None:
            start = point.second
        if (not point.paused) and start is not None:
            spans.append((start, point.second))
            start = None
        prev = point
    if start is not None and prev is not None:
        spans.append((start, prev.second + timedelta(seconds=1)))
    return spans
