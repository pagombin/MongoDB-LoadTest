"""Figure builders for dashboard and exports."""

from __future__ import annotations

from collections import defaultdict

import plotly.graph_objects as go

from mongo_load_tester.models import MetricsSnapshot

OP_COLORS = {
    "read": "#2563eb",
    "insert": "#10b981",
    "update": "#f59e0b",
    "delete": "#ef4444",
    "total": "#0f172a",
}


def build_operations_timeline(snapshot: MetricsSnapshot) -> go.Figure:
    x_values = [point.second for point in snapshot.timeline]
    read_counts = [point.op_counts.get("read", 0) for point in snapshot.timeline]
    insert_counts = [point.op_counts.get("insert", 0) for point in snapshot.timeline]
    update_counts = [point.op_counts.get("update", 0) for point in snapshot.timeline]
    delete_counts = [point.op_counts.get("delete", 0) for point in snapshot.timeline]
    total_counts = [point.total_ops for point in snapshot.timeline]

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=total_counts,
            mode="lines",
            name="total_ops",
            line={"width": 2.5, "color": OP_COLORS["total"]},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=read_counts,
            mode="lines",
            name="read_ops",
            line={"width": 2, "color": OP_COLORS["read"]},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=insert_counts,
            mode="lines",
            name="insert_ops",
            line={"width": 2, "color": OP_COLORS["insert"]},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=update_counts,
            mode="lines",
            name="update_ops",
            line={"width": 2, "color": OP_COLORS["update"]},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=delete_counts,
            mode="lines",
            name="delete_ops",
            line={"width": 2, "color": OP_COLORS["delete"]},
        )
    )
    figure.update_layout(
        title="Operations Timeline (per second)",
        xaxis_title="Time (UTC)",
        yaxis_title="Operations / sec",
        template="plotly_white",
        legend=dict(orientation="h"),
        hovermode="x unified",
        margin={"l": 40, "r": 20, "t": 50, "b": 40},
    )
    figure.update_yaxes(rangemode="tozero")
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
            line={"width": 2.5, "color": "#ef4444"},
        )
    )
    figure.update_layout(
        title="Latency Timeline",
        xaxis_title="Time (UTC)",
        yaxis_title="Latency (ms)",
        template="plotly_white",
        legend=dict(orientation="h"),
        hovermode="x unified",
        margin={"l": 40, "r": 20, "t": 50, "b": 40},
    )
    figure.update_yaxes(rangemode="tozero")
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
    figure.update_layout(title="Operation Distribution", template="plotly_white", margin={"l": 20, "r": 20, "t": 50, "b": 20})
    return figure
