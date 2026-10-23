"""Per-second measurement records shared by the generator, storage and analysis."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from ballast.engine.histogram import Histogram

WRITE_OPS = ("insert", "update", "upsert", "delete", "find_and_modify")
READ_OPS = ("find", "range", "aggregate", "analytics")
ALL_OPS = READ_OPS + WRITE_OPS
EXCLUDED_PHASES = ("load", "warmup", "paused", "stopping")


@dataclass(slots=True)
class OpSecond:
    ok: int = 0
    err: int = 0
    rt: Histogram = field(default_factory=Histogram)
    st: Histogram = field(default_factory=Histogram)

    def merge(self, other: OpSecond) -> None:
        self.ok += other.ok
        self.err += other.err
        self.rt.merge(other.rt)
        self.st.merge(other.st)

    def to_json(self) -> dict:
        return {"ok": self.ok, "err": self.err, "rt": self.rt.to_compact(), "st": self.st.to_compact()}

    @classmethod
    def from_json(cls, data: dict) -> OpSecond:
        return cls(
            ok=data.get("ok", 0),
            err=data.get("err", 0),
            rt=Histogram.from_compact(data.get("rt")),
            st=Histogram.from_compact(data.get("st")),
        )


@dataclass(slots=True)
class HostSecond:
    """Commands one member answered in one second, from driver command monitoring (each attempt counts)."""

    reads: int = 0
    writes: int = 0
    failed: int = 0
    st: Histogram = field(default_factory=Histogram)

    def merge(self, other: HostSecond) -> None:
        self.reads += other.reads
        self.writes += other.writes
        self.failed += other.failed
        self.st.merge(other.st)

    def to_json(self) -> list:
        return [self.reads, self.writes, self.failed, self.st.to_compact()]

    @classmethod
    def from_json(cls, data: list, keep_histogram: bool = True) -> HostSecond:
        return cls(int(data[0]), int(data[1]), int(data[2]), Histogram.from_compact(data[3]) if keep_histogram else Histogram())


@dataclass(slots=True)
class SecondRecord:
    t: int
    phase: str = "steady"
    target_rate: float | None = None
    concurrency: int = 0
    ops: dict[str, OpSecond] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    labels: dict[str, int] = field(default_factory=dict)
    first_err_ms: int | None = None
    last_err_ms: int | None = None
    pool_wait: Histogram = field(default_factory=Histogram)
    pool_cleared: int = 0
    checkout_failed: int = 0
    cpu: float = 0.0
    backlog: int = 0
    inflight: int = 0
    processes: int = 1
    span: int = 1
    hosts: dict[str, HostSecond] = field(default_factory=dict)
    op_counts: dict[str, list[int]] = field(default_factory=dict)

    @property
    def ok(self) -> int:
        return sum(o.ok for o in self.ops.values())

    @property
    def err(self) -> int:
        return sum(o.err for o in self.ops.values())

    def rt(self, ops: tuple[str, ...] | None = None) -> Histogram:
        hist = Histogram()
        for name, op in self.ops.items():
            if ops is None or name in ops:
                hist.merge(op.rt)
        return hist

    def st(self) -> Histogram:
        hist = Histogram()
        for op in self.ops.values():
            hist.merge(op.st)
        return hist

    def merge(self, other: SecondRecord) -> None:
        for name, op in other.ops.items():
            self.ops.setdefault(name, OpSecond()).merge(op)
        for key, value in other.errors.items():
            self.errors[key] = self.errors.get(key, 0) + value
        for key, value in other.labels.items():
            self.labels[key] = self.labels.get(key, 0) + value
        if other.first_err_ms is not None and (self.first_err_ms is None or other.first_err_ms < self.first_err_ms):
            self.first_err_ms = other.first_err_ms
        if other.last_err_ms is not None and (self.last_err_ms is None or other.last_err_ms > self.last_err_ms):
            self.last_err_ms = other.last_err_ms
        self.pool_wait.merge(other.pool_wait)
        self.pool_cleared += other.pool_cleared
        self.checkout_failed += other.checkout_failed
        self.cpu = max(self.cpu, other.cpu)
        self.backlog += other.backlog
        self.inflight += other.inflight
        self.concurrency += other.concurrency
        if other.target_rate is not None:
            self.target_rate = (self.target_rate or 0.0) + other.target_rate
        self.processes += other.processes
        for host, entry in other.hosts.items():
            self.hosts.setdefault(host, HostSecond()).merge(entry)
        for name, (ok, err) in other.op_counts.items():
            mine = self.op_counts.setdefault(name, [0, 0])
            mine[0] += ok
            mine[1] += err
        if other.phase != self.phase and other.phase == "paused":
            self.phase = other.phase

    def to_json(self) -> dict:
        return {
            "t": self.t,
            "phase": self.phase,
            "target_rate": self.target_rate,
            "concurrency": self.concurrency,
            "ops": {name: op.to_json() for name, op in self.ops.items()},
            "errors": self.errors,
            "labels": self.labels,
            "first_err_ms": self.first_err_ms,
            "last_err_ms": self.last_err_ms,
            "pool_wait": self.pool_wait.to_compact(),
            "pool_cleared": self.pool_cleared,
            "checkout_failed": self.checkout_failed,
            "cpu": self.cpu,
            "backlog": self.backlog,
            "inflight": self.inflight,
            "processes": self.processes,
            "span": self.span,
            "hosts": {host: entry.to_json() for host, entry in self.hosts.items()},
        }

    @classmethod
    def from_json(cls, data: dict, collapse: bool = False) -> SecondRecord:
        """With collapse, all operations become one "all" entry holding only response time; enough for
        impact analysis and several times smaller in memory."""
        if collapse:
            merged = OpSecond()
            for op in (data.get("ops") or {}).values():
                merged.ok += op.get("ok", 0)
                merged.err += op.get("err", 0)
                merged.rt.merge(Histogram.from_compact(op.get("rt")))
            merged.rt.pack()
            ops = {"all": merged} if data.get("ops") else {}
        else:
            ops = {name: OpSecond.from_json(op) for name, op in (data.get("ops") or {}).items()}
        return cls(
            t=int(data["t"]),
            phase=data.get("phase", "steady"),
            target_rate=data.get("target_rate"),
            concurrency=data.get("concurrency", 0),
            ops=ops,
            errors=dict(data.get("errors") or {}),
            labels=dict(data.get("labels") or {}),
            first_err_ms=data.get("first_err_ms"),
            last_err_ms=data.get("last_err_ms"),
            pool_wait=Histogram.from_compact(data.get("pool_wait")),
            pool_cleared=data.get("pool_cleared", 0),
            checkout_failed=data.get("checkout_failed", 0),
            cpu=data.get("cpu", 0.0),
            backlog=data.get("backlog", 0),
            inflight=data.get("inflight", 0),
            processes=data.get("processes", 1),
            span=data.get("span", 1),
            hosts={host: HostSecond.from_json(entry, not collapse) for host, entry in (data.get("hosts") or {}).items()},
            op_counts={name: [op.get("ok", 0), op.get("err", 0)] for name, op in (data.get("ops") or {}).items()} if collapse else {},
        )

    def collapsed(self) -> SecondRecord:
        """A copy with all operations folded into one response-time histogram (see from_json collapse)."""
        ops = {"all": OpSecond(ok=self.ok, err=self.err, rt=self.rt().pack())} if self.ops else {}
        hosts = {h: HostSecond(e.reads, e.writes, e.failed) for h, e in self.hosts.items()}
        counts = {name: [op.ok, op.err] for name, op in self.ops.items()}
        return dataclasses.replace(self, ops=ops, hosts=hosts, op_counts=counts)

    def point(self) -> dict:
        """Compact chart point for live updates and reports."""
        rt = self.rt()
        st = self.st()
        rtp = rt.percentiles((50, 99))
        stp = st.percentiles((50, 99))
        return {
            "t": self.t,
            "phase": self.phase,
            "ok": round(self.ok / self.span, 2),
            "err": round(self.err / self.span, 2),
            "target": self.target_rate,
            "p50": rtp[50],
            "p99": rtp[99],
            "max": rt.max,
            "st50": stp[50],
            "st99": stp[99],
            "errors": self.errors,
            "pool_wait_p99": self.pool_wait.percentile(99),
            "pool_cleared": self.pool_cleared,
            "cpu": round(self.cpu, 1),
            "backlog": self.backlog,
            "ops": {name: round((op.ok + op.err) / self.span, 2) for name, op in self.ops.items()},
            "hosts": {h: [round((e.reads + e.writes) / self.span, 2), round(e.failed / self.span, 2)] for h, e in self.hosts.items()},
        }


def merge_records(records: list[SecondRecord]) -> SecondRecord | None:
    if not records:
        return None
    base = SecondRecord.from_json(records[0].to_json())
    for record in records[1:]:
        base.merge(record)
    return base


def downsample(records: list[SecondRecord], step: int) -> list[SecondRecord]:
    """Merge consecutive records into buckets of ``step`` seconds (histograms merged, never averaged)."""
    if step <= 1:
        return records
    buckets: dict[int, list[SecondRecord]] = {}
    for record in records:
        buckets.setdefault(record.t - record.t % step, []).append(record)
    out: list[SecondRecord] = []
    for start in sorted(buckets):
        group = buckets[start]
        merged = merge_records(group)
        assert merged is not None
        merged.t = start
        merged.processes = group[0].processes
        merged.concurrency = max(r.concurrency for r in group)
        merged.target_rate = group[-1].target_rate
        merged.backlog = max(r.backlog for r in group)
        merged.inflight = max(r.inflight for r in group)
        merged.phase = group[-1].phase
        merged.span = step
        out.append(merged)
    return out
