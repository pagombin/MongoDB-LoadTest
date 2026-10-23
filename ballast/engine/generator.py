"""Generator process: runs one share of the workload and reports once per second.

Each generator process owns an asyncio loop and one PyMongo async client. It
receives commands from the server process over a pipe and sends back closed
per-second records, write/read gaps, pool events and load progress.

Timing: every operation records response time measured from its intended
start (open model) or actual start (closed model), and service time measured
from when it was actually sent. Both use the monotonic clock; the second an
operation belongs to is its completion time in UTC wall-clock seconds.
"""

from __future__ import annotations

import asyncio
import bisect
import math
import os
import random
import signal
import time
from typing import Any

from pymongo import AsyncMongoClient, ReturnDocument
from pymongo import errors as pe
from pymongo import monitoring

from ballast.engine.documents import DocumentFactory
from ballast.engine.errors import classify
from ballast.engine.keys import KeyChooser
from ballast.engine.profile import Profile
from ballast.engine.series import READ_OPS, WRITE_OPS, OpSecond, SecondRecord

GAP_MIN_MS = 100
CLOSE_GRACE_SECONDS = 0.15
RETRYABLE = (pe.AutoReconnect, pe.ConnectionFailure, pe.NotPrimaryError, pe.ServerSelectionTimeoutError, pe.NetworkTimeout)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, RETRYABLE):
        return True
    has_label = getattr(exc, "has_error_label", None)
    return bool(has_label and (has_label("RetryableWriteError") or has_label("RetryableReadError")))


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "****")
    return text


class OpenSchedule:
    """Fixed-interval schedule: operation i is intended to start at t0 + i / rate."""

    def __init__(self, rate: float, start: float):
        self.rate = rate
        self.t0 = start
        self.next_index = 0
        self.epoch = 0

    def take(self) -> float:
        intended = self.t0 + self.next_index / self.rate
        self.next_index += 1
        return intended

    def next_intended(self) -> float:
        return self.t0 + self.next_index / self.rate

    def set_rate(self, rate: float, now: float) -> None:
        self.t0 = max(self.next_intended(), now) if self.next_index else now
        self.next_index = 0
        self.rate = rate
        self.epoch += 1

    def shift(self, seconds: float) -> None:
        self.t0 += seconds
        self.epoch += 1

    def backlog(self, now: float) -> int:
        due = math.floor((now - self.t0) * self.rate) + 1
        return max(0, due - self.next_index)


class _PoolEvents(monitoring.ConnectionPoolListener):
    def __init__(self, gen: "Generator"):
        self.gen = gen

    def pool_created(self, event): pass
    def pool_ready(self, event): pass
    def pool_closed(self, event): pass
    def connection_created(self, event): pass
    def connection_ready(self, event): pass
    def connection_closed(self, event): pass
    def connection_check_out_started(self, event): pass
    def connection_checked_in(self, event): pass

    def pool_cleared(self, event):
        self.gen.on_pool_cleared(f"{event.address[0]}:{event.address[1]}")

    def connection_checked_out(self, event):
        self.gen.on_checkout(event.duration)

    def connection_check_out_failed(self, event):
        self.gen.on_checkout_failed(str(event.reason))


class Generator:
    def __init__(self, conn, spec: dict):
        self.conn = conn
        self.spec = spec
        self.profile = Profile.model_validate(spec["profile"])
        self.index = spec["process_index"]
        self.count = spec["process_count"]
        self.rng = random.Random(self.profile.seed * 1_000_003 + self.index)
        self.docs = DocumentFactory(self.profile.document, self.rng)
        self.collections_names: list[str] = spec["collections"]
        self.keys = {
            name: KeyChooser(self.profile.keys.distribution, int(spec["key_sizes"].get(name, 0)), self.rng,
                             self.profile.keys.zipf_theta, self.index, self.count)
            for name in self.collections_names
        }
        mix = self.profile.operations.normalized()
        self.op_names = list(mix)
        self.op_cum = []
        acc = 0.0
        for name in self.op_names:
            acc += mix[name]
            self.op_cum.append(acc)
        self.has_writes = any(name in WRITE_OPS for name in self.op_names)
        self.has_reads = any(name in READ_OPS for name in self.op_names)
        self.phase = "idle"
        self.running = False
        self.stopping = False
        self.paused = False
        self.resume_event = asyncio.Event()
        self.level_event = asyncio.Event()
        self.schedule: OpenSchedule | None = None
        self.target_workers = 0
        self.level = 0.0
        self.inflight = 0
        self.seconds: dict[int, SecondRecord] = {}
        self.last_emitted: int | None = None
        self.last_ok_ms = {"write": None, "read": None}
        self.kind_rate = {"write": 0.0, "read": 0.0}
        self.kind_count = {"write": 0, "read": 0}
        self.cpu_mark = (time.monotonic(), sum(os.times()[:2]))
        self.cpu = 0.0
        self.tasks: list[asyncio.Task] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self._error_samples: dict[str, int] = {}
        self.paused_at = 0.0
        secrets = [x for x in spec.get("secrets", []) if x]
        self.redact = (lambda text: _redact(text, secrets)) if secrets else None
        self.client: AsyncMongoClient | None = None
        self.db = None
        self.colls: list = []

    # ---- IPC ----

    def send(self, message: dict) -> None:
        try:
            self.conn.send(message)
        except (BrokenPipeError, OSError):
            self.stopping = True

    def _on_readable(self) -> None:
        try:
            while self.conn.poll():
                self.inbox.put_nowait(self.conn.recv())
        except (EOFError, OSError):
            self.inbox.put_nowait({"cmd": "stop", "drain": 1, "parent_gone": True})
            asyncio.get_running_loop().remove_reader(self.conn.fileno())

    # ---- recording ----

    def _second(self, t: int) -> SecondRecord:
        rec = self.seconds.get(t)
        if rec is None:
            rec = SecondRecord(t=t, phase="paused" if self.paused else self.phase)
            self.seconds[t] = rec
        return rec

    def on_checkout(self, duration: float) -> None:
        if self.running:
            self._second(int(time.time())).pool_wait.record(duration * 1_000_000)

    def on_checkout_failed(self, reason: str) -> None:
        if self.running:
            self._second(int(time.time())).checkout_failed += 1

    def on_pool_cleared(self, address: str) -> None:
        now = time.time()
        if self.running:
            self._second(int(now)).pool_cleared += 1
        self.send({"type": "event", "kind": "pool_cleared", "t_ms": int(now * 1000), "data": {"address": address, "process": self.index}})

    def record(self, op: str, ok: bool, rt_us: float, st_us: float, info, wall: float) -> None:
        rec = self._second(int(wall))
        entry = rec.ops.get(op)
        if entry is None:
            entry = rec.ops[op] = OpSecond()
        entry.rt.record(rt_us)
        entry.st.record(st_us)
        wall_ms = int(wall * 1000)
        kind = "write" if op in WRITE_OPS else "read"
        if ok:
            entry.ok += 1
            self.kind_count[kind] += 1
            last = self.last_ok_ms[kind]
            if last is not None:
                threshold = max(GAP_MIN_MS, 4000.0 / self.kind_rate[kind]) if self.kind_rate[kind] > 0 else GAP_MIN_MS
                if wall_ms - last >= threshold:
                    self.send({"type": "gap", "kind": kind, "start_ms": last, "end_ms": wall_ms, "process": self.index})
            self.last_ok_ms[kind] = wall_ms
        else:
            entry.err += 1
            rec.errors[info.error_class] = rec.errors.get(info.error_class, 0) + 1
            for label in info.labels:
                rec.labels[label] = rec.labels.get(label, 0) + 1
            if rec.first_err_ms is None or wall_ms < rec.first_err_ms:
                rec.first_err_ms = wall_ms
            if rec.last_err_ms is None or wall_ms > rec.last_err_ms:
                rec.last_err_ms = wall_ms
            self.send_error_sample(info, wall_ms)

    def send_error_sample(self, info, wall_ms: int) -> None:
        """Forward the first occurrence of each error class per 10 s so the timeline can name it."""
        key = info.error_class
        last = self._error_samples.get(key, 0)
        if wall_ms - last >= 10_000:
            self._error_samples[key] = wall_ms
            self.send({"type": "event", "kind": "error_sample", "t_ms": wall_ms, "data": {**info.as_dict(), "process": self.index}})

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            self._flush(time.time())

    def _flush(self, now: float, final: bool = False) -> None:
        if not self.running and not final:
            return
        limit = int(now - 1 - CLOSE_GRACE_SECONDS) if not final else int(now)
        start = self.last_emitted + 1 if self.last_emitted is not None else limit + 1
        mono = time.monotonic()
        cpu_total = sum(os.times()[:2])
        elapsed = mono - self.cpu_mark[0]
        if elapsed >= 0.5:
            self.cpu = 100.0 * (cpu_total - self.cpu_mark[1]) / elapsed
            self.cpu_mark = (mono, cpu_total)
        for t in range(start, limit + 1):
            rec = self.seconds.pop(t, None) or SecondRecord(t=t, phase="paused" if self.paused else self.phase)
            rec.cpu = self.cpu
            rec.inflight = self.inflight
            rec.processes = 1
            if self.profile.load.model == "open":
                rec.target_rate = 0.0 if self.paused or rec.phase == "paused" else self.level
                rec.concurrency = len(self.tasks)
                rec.backlog = self.schedule.backlog(mono) if self.schedule and not self.paused else 0
            else:
                rec.target_rate = None
                rec.concurrency = self.target_workers
            for kind in ("write", "read"):
                self.kind_rate[kind] = 0.7 * self.kind_rate[kind] + 0.3 * self.kind_count[kind]
                self.kind_count[kind] = 0
            self.send({"type": "second", "data": rec.to_json()})
            self.last_emitted = t
        for t in [t for t in self.seconds if self.last_emitted is not None and t <= self.last_emitted]:
            self.seconds.pop(t, None)

    # ---- operations ----

    def choose_op(self) -> str:
        return self.op_names[bisect.bisect_left(self.op_cum, self.rng.random() * self.op_cum[-1])] if len(self.op_names) > 1 else self.op_names[0]

    async def run_op(self, op: str, i: int) -> None:
        coll = self.colls[i]
        keys = self.keys[self.collections_names[i]]
        docs = self.docs
        if op == "find":
            await coll.find_one({"_id": keys.existing() or 0})
        elif op == "insert":
            await coll.insert_one(docs.new(keys.new_key()))
        elif op == "update":
            await coll.update_one({"_id": keys.existing() or 0}, docs.update())
        elif op == "upsert":
            key = keys.upsert_key()
            update = docs.update()
            update.setdefault("$setOnInsert", {"pad": docs.new(key).get("pad", "")})
            await coll.update_one({"_id": key}, update, upsert=True)
        elif op == "delete":
            await coll.delete_one({"_id": keys.existing() or 0})
        elif op == "range":
            start = keys.existing() or 0
            await coll.find({"_id": {"$gte": start}}).limit(self.profile.keys.range_size).to_list()
        elif op == "aggregate":
            start = keys.existing() or 0
            pipeline = [
                {"$match": {"_id": {"$gte": start, "$lt": start + self.profile.keys.range_size}}},
                {"$group": {"_id": None, "count": {"$sum": 1}, "total": {"$sum": "$v"}}},
            ]
            await (await coll.aggregate(pipeline)).to_list()
        elif op == "find_and_modify":
            await coll.find_one_and_update({"_id": keys.existing() or 0}, {"$inc": {"n": 1}}, return_document=ReturnDocument.AFTER)

    async def execute(self, intended: float) -> None:
        op = self.choose_op()
        i = self.rng.randrange(len(self.colls)) if len(self.colls) > 1 else 0
        retry = self.profile.retry
        self.inflight += 1
        start = time.monotonic()
        info = None
        ok = False
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    await self.run_op(op, i)
                    ok = True
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    info = classify(exc, self.redact)
                    if attempt >= retry.max_attempts or not is_retryable(exc) or self.stopping:
                        break
                    delay = min(retry.max_backoff_ms, retry.initial_backoff_ms * retry.backoff_multiplier ** (attempt - 1)) / 1000.0
                    await asyncio.sleep(delay * self.rng.uniform(0.8, 1.2))
        finally:
            self.inflight -= 1
        end = time.monotonic()
        self.record(op, ok, (end - intended) * 1e6, (end - start) * 1e6, info, time.time())

    # ---- workers ----

    async def open_worker(self) -> None:
        while not self.stopping:
            if self.paused:
                await self.resume_event.wait()
                continue
            sched = self.schedule
            epoch = sched.epoch
            intended = sched.take()
            delay = intended - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
                if self.stopping or self.paused or sched.epoch != epoch:
                    continue
            await self.execute(intended)

    async def closed_worker(self, worker_id: int) -> None:
        think = self.profile.load.think_time_ms / 1000.0
        while not self.stopping:
            if self.paused:
                await self.resume_event.wait()
                continue
            if worker_id >= self.target_workers:
                self.level_event.clear()
                await self.level_event.wait()
                continue
            await self.execute(time.monotonic())
            if think > 0:
                await asyncio.sleep(think * self.rng.uniform(0.5, 1.5))

    def share(self, level: float) -> float:
        if self.profile.load.model == "open":
            return level / self.count
        base, extra = divmod(int(round(level)), self.count)
        return float(base + (1 if self.index < extra else 0))

    def apply_level(self, level: float, phase: str) -> None:
        self.phase = phase
        share = self.share(level)
        self.level = share
        now = time.monotonic()
        if self.profile.load.model == "open":
            if share <= 0:
                share = 0.001
            if self.schedule is None:
                self.schedule = OpenSchedule(share, now)
                concurrency = max(1, self.profile.load.concurrency // self.count + (1 if self.index < self.profile.load.concurrency % self.count else 0))
                self.tasks = [asyncio.create_task(self.open_worker()) for _ in range(concurrency)]
            else:
                self.schedule.set_rate(share, now)
        else:
            target = int(share)
            while len(self.tasks) < target:
                self.tasks.append(asyncio.create_task(self.closed_worker(len(self.tasks))))
            self.target_workers = target
            self.level_event.set()

    def start_measuring(self) -> None:
        now_ms = int(time.time() * 1000)
        self.last_ok_ms = {"write": now_ms if self.has_writes else None, "read": now_ms if self.has_reads else None}

    def close_open_gaps(self) -> None:
        now_ms = int(time.time() * 1000)
        for kind, last in self.last_ok_ms.items():
            if last is not None and now_ms - last >= GAP_MIN_MS * 10:
                self.send({"type": "gap", "kind": kind, "start_ms": last, "end_ms": now_ms, "process": self.index, "open": True})

    # ---- data load ----

    async def load(self, targets: dict[str, int], batch_size: int) -> None:
        inserted = 0
        last_report = time.monotonic()
        for i, name in enumerate(self.collections_names):
            chooser = self.keys[name]
            have = chooser.size
            want = int(targets.get(name, 0))
            if want <= have:
                continue
            span = want - have
            per = span // self.count
            lo = have + per * self.index + min(self.index, span % self.count)
            hi = lo + per + (1 if self.index < span % self.count else 0)
            key = lo
            while key < hi and not self.stopping:
                batch = [self.docs.new(k) for k in range(key, min(hi, key + batch_size))]
                try:
                    await self.colls[i].insert_many(batch, ordered=False)
                except pe.BulkWriteError as exc:
                    if any(e.get("code") != 11000 for e in exc.details.get("writeErrors", [])):
                        raise
                key += len(batch)
                inserted += len(batch)
                if time.monotonic() - last_report >= 0.5:
                    self.send({"type": "load_progress", "docs": inserted, "process": self.index})
                    last_report = time.monotonic()
            for chooser_name in (name,):
                c = self.keys[chooser_name]
                c.size = max(c.size, want)
                c._next_insert = max(c._next_insert, c.size + self.index)
        self.send({"type": "load_progress", "docs": inserted, "process": self.index})
        self.send({"type": "load_done", "process": self.index, "docs": inserted})

    # ---- main ----

    async def main(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_reader(self.conn.fileno(), self._on_readable)
        options = dict(self.spec.get("client_options") or {})
        options.update(self.profile.driver.client_options())
        options["appname"] = self.spec.get("app_name", "ballast")
        try:
            self.client = AsyncMongoClient(self.spec["uri"], event_listeners=[_PoolEvents(self)], **options)
            await self.client.admin.command("ping")
            self.db = self.client[self.spec["database"]]
            self.colls = [self.db[name] for name in self.collections_names]
        except Exception as exc:
            self.send({"type": "error", "stage": "connect", "error": classify(exc, self.redact).as_dict()})
            return
        self.send({"type": "ready", "process": self.index, "pid": os.getpid()})
        flusher: asyncio.Task | None = None
        load_task: asyncio.Task | None = None
        try:
            while True:
                msg = await self.inbox.get()
                cmd = msg.get("cmd")
                if cmd == "load":
                    load_task = asyncio.create_task(self._guard(self.load(msg["targets"], msg.get("batch_size", 1000)), "load"))
                elif cmd == "run":
                    self.running = True
                    self.last_emitted = int(time.time()) - 1
                    self.start_measuring()
                    self.apply_level(msg["level"], msg.get("phase", "steady"))
                    if flusher is None:
                        flusher = asyncio.create_task(self._flush_loop())
                elif cmd == "set":
                    self.apply_level(msg["level"], msg.get("phase", self.phase))
                elif cmd == "pause" and not self.paused:
                    self._second(int(time.time())).phase = "paused"
                    self.paused = True
                    self.paused_at = time.monotonic()
                    self.resume_event.clear()
                elif cmd == "resume" and self.paused:
                    if self.schedule is not None:
                        self.schedule.shift(time.monotonic() - self.paused_at)
                    self.paused = False
                    self._second(int(time.time())).phase = "paused"
                    self.start_measuring()
                    self.resume_event.set()
                elif cmd == "stop":
                    await self._stop(float(msg.get("drain", 10)))
                    break
        finally:
            if load_task is not None and not load_task.done():
                load_task.cancel()
            if flusher is not None:
                flusher.cancel()
            try:
                await self.client.close()
            except Exception:
                pass
            self.send({"type": "stopped", "process": self.index})

    async def _guard(self, coro, stage: str) -> None:
        try:
            await coro
        except Exception as exc:
            self.send({"type": "error", "stage": stage, "error": classify(exc, self.redact).as_dict()})

    async def _stop(self, drain: float) -> None:
        self.stopping = True
        self.phase = "stopping"
        for rec in self.seconds.values():
            if rec.t >= int(time.time()):
                rec.phase = "stopping"
        self.resume_event.set()
        self.level_event.set()
        deadline = time.monotonic() + drain
        while self.inflight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        forced = self.inflight
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.running:
            self.close_open_gaps()
            self._flush(time.time(), final=True)
        self.running = False
        self.send({"type": "drained", "process": self.index, "forced": forced})


def entry(conn, spec: dict) -> None:
    """multiprocessing target."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        asyncio.run(Generator(conn, spec).main())
    finally:
        try:
            conn.close()
        except Exception:
            pass
