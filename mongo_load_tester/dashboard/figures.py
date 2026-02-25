"""Figure builders for dashboard and exports."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

import plotly.graph_objects as go

from mongo_load_tester.models import MetricsSnapshot

OP_COLORS = {
    "read": "#3b82f6",
    "insert": "#22c55e",
    "update": "#f59e0b",
    "delete": "#ef4444",
    "find_and_modify": "#8b5cf6",
    "total": "#111827",
    "users": "#a855f7",
}

_MAX_PAUSE_OVERLAYS = 50


def _apply_timeline_layout(
    figure: go.Figure,
    yaxis_title: str,
    x_range: tuple[datetime, datetime] | None = None,
    bottom_margin: int = 50,
    x_title: str = "",
) -> None:
    """Shared layout configuration for all timeline figures."""
    figure.update_layout(
        title=None,
        xaxis={"title": x_title, "title_standoff": 0 if not x_title else 16},
        yaxis_title=yaxis_title,
        yaxis_title_font={"size": 12, "color": "#4b5563"},
        template="plotly_white",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#e5e7eb",
            borderwidth=1,
            font={"size": 11},
        ),
        hovermode="x unified",
        hoverlabel={"bgcolor": "white", "bordercolor": "#d1d5db", "font_size": 12},
        margin={"l": 50, "r": 24, "t": 36, "b": bottom_margin},
        plot_bgcolor="#fafbff",
        paper_bgcolor="white",
    )
    figure.update_xaxes(
        showgrid=True, gridcolor="#eef2f7", gridwidth=1,
        zeroline=False, tickfont={"size": 10, "color": "#6b7280"},
        showline=True, linewidth=1, linecolor="#e2e8f0",
    )
    figure.update_yaxes(
        rangemode="tozero", showgrid=True, gridcolor="#eef2f7", gridwidth=1,
        tickfont={"size": 10, "color": "#6b7280"},
        showline=True, linewidth=1, linecolor="#e2e8f0",
    )
    if x_range is not None:
        figure.update_xaxes(range=list(x_range))


def build_operations_timeline(snapshot: MetricsSnapshot, x_range: tuple[datetime, datetime] | None = None) -> go.Figure:
    timeline = snapshot.timeline
    x_values = [point.second for point in timeline]
    total_counts = [point.total_ops for point in timeline]
    read_counts: list[int] = []
    write_counts: list[int] = []
    for point in timeline:
        read_counts.append(point.op_counts.get("read", 0))
        write_counts.append(
            point.op_counts.get("insert", 0)
            + point.op_counts.get("update", 0)
            + point.op_counts.get("delete", 0)
        )

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=total_counts,
            mode="lines",
            name="total_ops",
            line={"width": 2.1, "color": OP_COLORS["total"], "shape": "spline", "smoothing": 0.55},
            hovertemplate="Total ops: %{y}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=read_counts,
            mode="lines",
            name="read_ops",
            line={"width": 1.7, "color": OP_COLORS["read"], "shape": "spline", "smoothing": 0.5},
            hovertemplate="Read ops: %{y}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=write_counts,
            mode="lines",
            name="write_ops",
            line={"width": 1.7, "color": OP_COLORS["insert"], "shape": "spline", "smoothing": 0.5},
            fill="tozeroy",
            fillcolor="rgba(34, 197, 94, 0.08)",
            hovertemplate="Write ops: %{y}<extra></extra>",
        )
    )
    _apply_timeline_layout(figure, yaxis_title="Operations / sec", x_range=x_range)
    _add_pause_overlays(figure, snapshot)
    return figure


def build_latency_timeline(snapshot: MetricsSnapshot, x_range: tuple[datetime, datetime] | None = None) -> go.Figure:
    x_values = [point.second for point in snapshot.timeline]
    p50_values = [point.p50_latency_ms for point in snapshot.timeline]
    avg_values = [point.avg_latency_ms for point in snapshot.timeline]
    p95_values = [point.p95_latency_ms for point in snapshot.timeline]
    p99_values = [point.p99_latency_ms for point in snapshot.timeline]

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=x_values, y=p99_values, mode="lines", name="p99",
            line={"width": 1.2, "color": "#9333ea", "dash": "dot", "shape": "spline", "smoothing": 0.45},
            hovertemplate="P99: %{y:.2f} ms<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values, y=p95_values, mode="lines", name="p95",
            line={"width": 1.7, "color": "#ec4899", "shape": "spline", "smoothing": 0.45},
            hovertemplate="P95: %{y:.2f} ms<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values, y=avg_values, mode="lines", name="avg",
            line={"width": 1.9, "color": "#2563eb", "shape": "spline", "smoothing": 0.45},
            fill="tozeroy", fillcolor="rgba(37, 99, 235, 0.1)",
            hovertemplate="Avg: %{y:.2f} ms<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values, y=p50_values, mode="lines", name="p50 (median)",
            line={"width": 1.3, "color": "#059669", "dash": "dash", "shape": "spline", "smoothing": 0.45},
            hovertemplate="P50: %{y:.2f} ms<extra></extra>",
        )
    )
    _apply_timeline_layout(figure, yaxis_title="Latency (ms)", x_range=x_range)
    _add_pause_overlays(figure, snapshot)
    return figure


def build_user_ramp_timeline(snapshot: MetricsSnapshot, x_range: tuple[datetime, datetime] | None = None) -> go.Figure:
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
            line={"width": 1.8, "color": OP_COLORS["users"], "shape": "hv"},
            fill="tozeroy",
            fillcolor="rgba(139, 92, 246, 0.18)",
            hovertemplate="Active users: %{y}<extra></extra>",
        )
    )
    if any(value > 0 for value in target_users):
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=target_users,
                mode="lines",
                name="target_users",
                line={"width": 1.3, "color": "#334155", "dash": "dash"},
                hovertemplate="Target users: %{y}<extra></extra>",
            )
        )
    _apply_timeline_layout(
        figure, yaxis_title="Active virtual users", x_range=x_range,
        bottom_margin=70, x_title="Time (UTC)",
    )
    _add_pause_overlays(figure, snapshot)
    return figure


def build_operation_distribution(snapshot: MetricsSnapshot) -> go.Figure:
    counts = defaultdict(int, snapshot.operation_counts)
    labels = ["read", "insert", "update", "delete", "find_and_modify"]
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
                marker={"colors": [OP_COLORS["read"], OP_COLORS["insert"], OP_COLORS["update"], OP_COLORS["delete"], OP_COLORS["find_and_modify"]]},
            )
        ]
    )
    figure.update_layout(
        title="Operation Distribution",
        template="plotly_white",
        margin={"l": 20, "r": 20, "t": 50, "b": 20},
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        plot_bgcolor="#f8fafc",
        paper_bgcolor="white",
    )
    return figure


def _add_pause_overlays(figure: go.Figure, snapshot: MetricsSnapshot) -> None:
    pause_spans = _pause_spans(snapshot)
    if not pause_spans:
        return
    for start, end in pause_spans[:_MAX_PAUSE_OVERLAYS]:
        figure.add_vrect(
            x0=start,
            x1=end,
            fillcolor="rgba(245, 158, 11, 0.14)",
            line_width=0,
            annotation_text="paused",
            annotation_position="top left",
        )


def build_error_retry_timeline(snapshot: MetricsSnapshot, x_range: tuple[datetime, datetime] | None = None) -> go.Figure:
    """Error count and retry count per second, dual-axis."""
    x_values = [point.second for point in snapshot.timeline]
    error_counts = [point.error_ops for point in snapshot.timeline]
    retry_counts = [point.retried_ops for point in snapshot.timeline]
    error_pct = [
        (point.error_ops / point.total_ops * 100.0) if point.total_ops else 0.0
        for point in snapshot.timeline
    ]

    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=x_values,
            y=error_counts,
            name="errors/sec",
            marker_color="rgba(239, 68, 68, 0.7)",
            hovertemplate="Errors: %{y}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Bar(
            x=x_values,
            y=retry_counts,
            name="retries/sec",
            marker_color="rgba(251, 191, 36, 0.6)",
            hovertemplate="Retries: %{y}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=error_pct,
            mode="lines",
            name="error %",
            line={"width": 1.8, "color": "#991b1b", "shape": "spline", "smoothing": 0.4},
            yaxis="y2",
            hovertemplate="Error rate: %{y:.2f}%<extra></extra>",
        )
    )
    figure.update_layout(
        title=None,
        xaxis={"title": "", "title_standoff": 0},
        yaxis={"title": "Count / sec", "rangemode": "tozero"},
        yaxis2={
            "title": "Error %",
            "overlaying": "y",
            "side": "right",
            "rangemode": "tozero",
            "showgrid": False,
        },
        template="plotly_white",
        barmode="group",
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
            bgcolor="rgba(255,255,255,0.72)", bordercolor="#e5e7eb", borderwidth=1,
        ),
        hovermode="x unified",
        margin={"l": 42, "r": 50, "t": 44, "b": 50},
        plot_bgcolor="#f8fafc",
        paper_bgcolor="white",
    )
    figure.update_xaxes(showgrid=True, gridcolor="#e2e8f0", zeroline=False, tickfont={"size": 11})
    if x_range is not None:
        figure.update_xaxes(range=list(x_range))
    _add_pause_overlays(figure, snapshot)
    return figure


def build_per_op_throughput_timeline(snapshot: MetricsSnapshot, x_range: tuple[datetime, datetime] | None = None) -> go.Figure:
    """Individual operation type throughput over time."""
    x_values = [point.second for point in snapshot.timeline]

    figure = go.Figure()
    for op_type, color in [("read", OP_COLORS["read"]), ("insert", OP_COLORS["insert"]),
                           ("update", OP_COLORS["update"]), ("delete", OP_COLORS["delete"]),
                           ("find_and_modify", OP_COLORS["find_and_modify"])]:
        counts = [point.op_counts.get(op_type, 0) for point in snapshot.timeline]
        if not any(c > 0 for c in counts):
            continue
        figure.add_trace(
            go.Scatter(
                x=x_values, y=counts, mode="lines", name=f"{op_type}_ops",
                line={"width": 1.8, "color": color, "shape": "spline", "smoothing": 0.5},
                hovertemplate=f"{op_type.capitalize()} ops: %{{y}}<extra></extra>",
            )
        )
    _apply_timeline_layout(figure, yaxis_title="Operations / sec", x_range=x_range)
    _add_pause_overlays(figure, snapshot)
    return figure


def build_per_op_latency_timeline(snapshot: MetricsSnapshot, x_range: tuple[datetime, datetime] | None = None) -> go.Figure:
    """Average latency per operation type over time."""
    x_values = [point.second for point in snapshot.timeline]

    figure = go.Figure()
    for op_type, color in [("read", OP_COLORS["read"]), ("insert", OP_COLORS["insert"]),
                           ("update", OP_COLORS["update"]), ("delete", OP_COLORS["delete"]),
                           ("find_and_modify", OP_COLORS["find_and_modify"])]:
        values = [point.op_latency_avg.get(op_type, 0.0) for point in snapshot.timeline]
        if any(v > 0 for v in values):
            figure.add_trace(
                go.Scatter(
                    x=x_values, y=values, mode="lines", name=f"{op_type}_latency",
                    line={"width": 1.7, "color": color, "shape": "spline", "smoothing": 0.45},
                    hovertemplate=f"{op_type.capitalize()} avg: %{{y:.2f}} ms<extra></extra>",
                )
            )
    _apply_timeline_layout(figure, yaxis_title="Avg latency (ms)", x_range=x_range)
    _add_pause_overlays(figure, snapshot)
    return figure


def build_latency_histogram(snapshot: MetricsSnapshot) -> go.Figure:
    """Latency distribution histogram from global reservoir."""
    histogram = snapshot.latency_histogram
    if not histogram:
        figure = go.Figure()
        figure.add_annotation(
            text="No latency data captured yet",
            x=0.5, y=0.5, showarrow=False,
            font={"size": 14, "color": "#6b7280"},
        )
        figure.update_layout(
            title="Latency Distribution", template="plotly_white",
            xaxis={"visible": False}, yaxis={"visible": False},
            margin={"l": 20, "r": 20, "t": 50, "b": 20},
            plot_bgcolor="#f8fafc", paper_bgcolor="white",
        )
        return figure

    labels = [f"≤{b:.0f}ms" if b < 1000 else f"≤{b/1000:.1f}s" for b, _ in histogram]
    counts = [c for _, c in histogram]
    total = sum(counts)
    cumulative_pct: list[float] = []
    running = 0
    for c in counts:
        running += c
        cumulative_pct.append((running / total * 100.0) if total else 0.0)

    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=labels, y=counts, name="count",
            marker_color="rgba(99, 102, 241, 0.7)",
            hovertemplate="Bucket %{x}: %{y} ops<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=labels, y=cumulative_pct, mode="lines+markers",
            name="cumulative %", yaxis="y2",
            line={"width": 2, "color": "#dc2626"},
            marker={"size": 5},
            hovertemplate="Cumulative: %{y:.1f}%<extra></extra>",
        )
    )
    figure.update_layout(
        title="Latency Distribution",
        xaxis={"title": "Latency bucket"},
        yaxis={"title": "Operation count", "rangemode": "tozero"},
        yaxis2={
            "title": "Cumulative %", "overlaying": "y", "side": "right",
            "rangemode": "tozero", "range": [0, 105], "showgrid": False,
        },
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin={"l": 42, "r": 50, "t": 50, "b": 60},
        plot_bgcolor="#f8fafc", paper_bgcolor="white",
    )
    return figure


def build_error_breakdown(snapshot: MetricsSnapshot) -> go.Figure:
    """Pie chart of error categories."""
    categories = snapshot.error_categories
    if not categories or sum(categories.values()) == 0:
        figure = go.Figure()
        figure.add_annotation(
            text="No errors captured",
            x=0.5, y=0.5, showarrow=False,
            font={"size": 14, "color": "#6b7280"},
        )
        figure.update_layout(
            title="Error Breakdown", template="plotly_white",
            xaxis={"visible": False}, yaxis={"visible": False},
            margin={"l": 20, "r": 20, "t": 50, "b": 20},
            plot_bgcolor="#f8fafc", paper_bgcolor="white",
        )
        return figure

    _CATEGORY_COLORS = {
        "timeout": "#f59e0b", "network": "#ef4444", "primary_stepdown": "#dc2626",
        "server_selection": "#f97316", "duplicate_key": "#8b5cf6",
        "cursor": "#06b6d4", "write_concern": "#ec4899", "auth": "#6366f1",
        "other": "#6b7280", "unknown": "#9ca3af",
    }
    labels = list(categories.keys())
    values = [categories[k] for k in labels]
    colors = [_CATEGORY_COLORS.get(k, "#6b7280") for k in labels]

    figure = go.Figure(
        data=[go.Pie(
            labels=labels, values=values, hole=0.45,
            marker={"colors": colors},
            textinfo="label+percent",
        )]
    )
    figure.update_layout(
        title="Error Breakdown", template="plotly_white",
        margin={"l": 20, "r": 20, "t": 50, "b": 20},
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        plot_bgcolor="#f8fafc", paper_bgcolor="white",
    )
    return figure


def _pause_spans(snapshot: MetricsSnapshot) -> list[tuple[datetime, datetime]]:
    spans: list[tuple[datetime, datetime]] = []
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
