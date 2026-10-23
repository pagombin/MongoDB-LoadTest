"""Soak test: a long run at a moderate rate while sampling memory and file descriptors.

Usage: runtime/python/bin/python3 tests/soak/soak.py --base https://127.0.0.1:9543 --uri mongodb://... --seconds 7500 --out soak.csv
Samples RSS and open file descriptors of the server process and its generator processes every 30 s (Linux /proc).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import time
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def api(base: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json", "X-Ballast-Request": "1"})
    with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read())


def proc_stats(pid: int) -> tuple[int, int]:
    rss = 0
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
    return rss, len(os.listdir(f"/proc/{pid}/fd"))


def children(pid: int) -> list[int]:
    out = []
    for task in os.listdir(f"/proc/{pid}/task"):
        try:
            out += [int(c) for c in open(f"/proc/{pid}/task/{task}/children").read().split()]
        except OSError:
            pass
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--uri", required=True)
    p.add_argument("--seconds", type=int, default=7500)
    p.add_argument("--rate", type=float, default=1000)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    target = api(a.base, "POST", "/api/targets", {"name": f"soak-{int(time.time())}", "uri": a.uri, "tls": "off"})
    profile = {"name": "soak", "load": {"model": "open", "rate": a.rate, "concurrency": 128, "processes": 2},
               "operations": {"find": 50, "update": 30, "insert": 15, "range": 5},
               "schedule": {"warmup_seconds": 30, "steady_seconds": a.seconds},
               "dataset": {"database": "ballast_soak", "collections": 4, "load": {"documents": 50000}}}
    run = api(a.base, "POST", "/api/runs", {"name": "soak", "target_id": target["id"], "profile": profile, "acknowledge_foreign": True})
    print("run", run["id"], flush=True)
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["elapsed_s", "status", "server_rss_kb", "server_fds", "generators_rss_kb", "generators_fds", "generator_count"])
        start = time.time()
        while True:
            status = api(a.base, "GET", f"/api/runs/{run['id']}")["status"]
            srss, sfd = proc_stats(a.pid)
            grss = gfd = 0
            kids = [c for c in children(a.pid)]
            for c in kids:
                try:
                    r, d = proc_stats(c)
                    grss += r
                    gfd += d
                except OSError:
                    pass
            w.writerow([int(time.time() - start), status, srss, sfd, grss, gfd, len(kids)])
            f.flush()
            if status not in ("starting", "loading", "running", "stopping"):
                break
            time.sleep(30)
    print("finished", status, flush=True)


if __name__ == "__main__":
    main()
