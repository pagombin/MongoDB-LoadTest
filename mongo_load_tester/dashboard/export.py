"""Browser export helpers for CSV/PNG/PDF."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from plotly.graph_objs import Figure
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from mongo_load_tester.models import MetricsSnapshot


def build_csv_bytes(rows: list[dict[str, str | int | float]]) -> bytes:
    """Serialize timeline rows to CSV bytes."""

    output = io.StringIO()
    if rows:
        fieldnames = list(rows[0].keys())
    else:
        fieldnames = [
            "timestamp_utc",
            "total_ops",
            "error_ops",
            "retried_ops",
            "avg_attempts_per_op",
            "avg_latency_ms",
            "p95_latency_ms",
            "read_ops",
            "insert_ops",
            "update_ops",
            "delete_ops",
            "active_virtual_users",
        ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def build_png_bytes(figure: Figure, width: int, height: int) -> bytes:
    """Render a Plotly figure as PNG."""

    return figure.to_image(format="png", width=width, height=height, scale=2)


def build_pdf_report(
    snapshot: MetricsSnapshot,
    ops_figure: Figure,
    latency_figure: Figure,
    users_figure: Figure,
    image_width: int = 1200,
    image_height: int = 450,
) -> bytes:
    """Generate a PDF report containing summary + timeline charts."""

    pdf_buffer = io.BytesIO()
    pdf_canvas = canvas.Canvas(pdf_buffer, pagesize=landscape(letter))
    page_width, page_height = landscape(letter)

    now = datetime.now(UTC).isoformat()
    pdf_canvas.setFont("Helvetica-Bold", 16)
    pdf_canvas.drawString(30, page_height - 30, "MongoDB Load Test Report")
    pdf_canvas.setFont("Helvetica", 10)
    pdf_canvas.drawString(30, page_height - 48, f"Generated: {now}")

    summary_lines = [
        f"Elapsed (s): {snapshot.elapsed_seconds:.2f}",
        f"Total ops: {snapshot.total_ops}",
        f"Success ops: {snapshot.success_ops}",
        f"Error ops: {snapshot.error_ops}",
        f"Ops/sec: {snapshot.ops_per_second:.2f}",
        f"Avg latency ms: {snapshot.avg_latency_ms:.2f}",
        f"P50 latency ms: {snapshot.p50_latency_ms:.2f}",
        f"P95 latency ms: {snapshot.p95_latency_ms:.2f}",
        f"P99 latency ms: {snapshot.p99_latency_ms:.2f}",
        f"Active users (latest): {snapshot.active_virtual_users_current}",
        f"Retried operations: {snapshot.retried_ops}",
        f"Average attempts/op: {snapshot.avg_attempts_per_op:.3f}",
    ]
    text_y = page_height - 66
    for line in summary_lines:
        pdf_canvas.drawString(30, text_y, line)
        text_y -= 14

    chart_width = min(page_width - 60, image_width * 0.6)
    chart_height = min((page_height - 230) / 2, image_height * 0.4)
    ops_png = build_png_bytes(ops_figure, image_width, image_height)
    lat_png = build_png_bytes(latency_figure, image_width, image_height)
    users_png = build_png_bytes(users_figure, image_width, image_height)

    ops_img = ImageReader(io.BytesIO(ops_png))
    lat_img = ImageReader(io.BytesIO(lat_png))
    users_img = ImageReader(io.BytesIO(users_png))
    first_chart_y = max(120, text_y - chart_height - 20)
    second_chart_y = max(20, first_chart_y - chart_height - 20)
    pdf_canvas.drawImage(ops_img, 30, first_chart_y, width=chart_width, height=chart_height)
    pdf_canvas.drawImage(lat_img, 30, second_chart_y, width=chart_width, height=chart_height)
    pdf_canvas.showPage()

    pdf_canvas.setFont("Helvetica-Bold", 14)
    pdf_canvas.drawString(30, page_height - 30, "Users Ramp Timeline")
    user_chart_width = min(page_width - 60, image_width * 0.72)
    user_chart_height = min(page_height - 100, image_height * 0.58)
    pdf_canvas.drawImage(users_img, 30, page_height - user_chart_height - 60, width=user_chart_width, height=user_chart_height)
    pdf_canvas.save()
    return pdf_buffer.getvalue()
