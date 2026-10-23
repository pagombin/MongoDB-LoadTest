"""Draws the soak CSV as an SVG: memory (MB) and open file descriptors over time, server and generators.

Usage: python tests/soak/plot.py soak.csv soak.svg
"""

from __future__ import annotations

import csv
import sys

W, H = 900, 560
LEFT, RIGHT = 70, 30
PANELS = [
    ("Resident memory (MB)", "server_rss_kb", "generators_rss_kb", 1 / 1024),
    ("Open file descriptors", "server_fds", "generators_fds", 1),
]
COLORS = {"server": "#1f6f8b", "generators": "#c2621a"}


def nice_max(value: float) -> float:
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
        top = step * (int(value / step) + 1)
        if top / step <= 6:
            return top
    return value * 1.1


def panel(rows: list[dict], title: str, server_key: str, gen_key: str, scale: float, top: int, height: int) -> list[str]:
    t_max = max(int(r["elapsed_s"]) for r in rows) or 1
    series = {
        "server": [(int(r["elapsed_s"]), int(r[server_key]) * scale) for r in rows],
        "generators": [(int(r["elapsed_s"]), int(r[gen_key]) * scale) for r in rows if int(r["generator_count"]) > 0],
    }
    y_max = nice_max(max(v for points in series.values() for _, v in points))
    plot_w = W - LEFT - RIGHT

    def x(t: float) -> float:
        return LEFT + plot_w * t / t_max

    def y(v: float) -> float:
        return top + height - height * v / y_max

    out = [f'<text x="{LEFT}" y="{top - 12}" class="title">{title}</text>']
    for i in range(6):
        v = y_max * i / 5
        out.append(f'<line x1="{LEFT}" x2="{W - RIGHT}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>')
        out.append(f'<text x="{LEFT - 8}" y="{y(v) + 4:.1f}" class="tick" text-anchor="end">{v:g}</text>')
    for minutes in range(0, t_max // 60 + 1, 15):
        out.append(f'<text x="{x(minutes * 60):.1f}" y="{top + height + 16}" class="tick" text-anchor="middle">{minutes} min</text>')
    for name, points in series.items():
        if points:
            path = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in points)
            out.append(f'<polyline points="{path}" fill="none" stroke="{COLORS[name]}" stroke-width="2"/>')
    return out


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f:
        rows = [r for r in csv.DictReader(f) if r["status"] == "running"]
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="sans-serif">',
        "<style>.title{font-size:14px;font-weight:600;fill:#1b1d21}.tick{font-size:11px;fill:#5b616b}.grid{stroke:#e3e5e8}</style>",
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
    ]
    body += panel(rows, *PANELS[0][:1], *PANELS[0][1:], top=50, height=180)
    body += panel(rows, *PANELS[1][:1], *PANELS[1][1:], top=320, height=180)
    for i, (name, color) in enumerate(COLORS.items()):
        lx = W - RIGHT - 220 + i * 110
        body.append(f'<rect x="{lx}" y="14" width="12" height="3" fill="{color}"/>')
        body.append(f'<text x="{lx + 18}" y="20" class="tick">{name}</text>')
    body.append("</svg>")
    with open(dst, "w") as f:
        f.write("\n".join(body) + "\n")


if __name__ == "__main__":
    main()
