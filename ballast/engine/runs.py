"""Run lifecycle in the server process: preflight, data load, schedule, aggregation, persistence."""

from __future__ import annotations

import asyncio
import logging
import math
import multiprocessing
import os
import platform
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from multiprocessing.connection import Connection
from typing import Any

import pymongo
from pymongo import AsyncMongoClient

from ballast import product
from ballast.engine import analysis
from ballast.engine.analysis import AnalysisSettings
from ballast.engine.documents import DocumentFactory
from ballast.engine.monitor import ClusterMonitor
from ballast.engine.profile import Profile
from ballast.engine.series import SecondRecord, downsample
from ballast.notify import Notifier
from ballast.store import Store, intersect_gaps, new_id, now_ms
from ballast.targets import MASK, client_options, explain_failure, redact_uri, secrets_in

UTC = timezone.utc

log = logging.getLogger("ballast.runs")

ACTIVE = ("starting", "loading", "running", "paused", "stopping")
LIVE_POINTS = 3600
STRAGGLER_SECONDS = 3


class RunError(Exception):
    def __init__(self, title: str, detail: str = "", fix: str = "", status: int = 400, extra: dict | None = None):
        super().__init__(title)
        self.title, self.detail, self.fix, self.status, self.extra = title, detail, fix, status, extra or {}

    def as_dict(self) -> dict:
        return {"title": self.title, "detail": self.detail, "fix": self.fix, **self.extra}


class Hub:
    """Fan-out of live messages to server-sent event subscribers."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def publish(self, kind: str, data: dict) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait((kind, data))
            except asyncio.QueueFull:
                self.subscribers.discard(queue)


@dataclass
class ProcessHandle:
    index: int
    process: multiprocessing.Process
    conn: Connection
    ready: bool = False
    drained: bool = False
    stopped: bool = False
    loaded: int = 0
    load_done: bool = False


@dataclass
class ActiveRun:
    id: str
    name: str
    profile: Profile
    target: dict
    database: str
    collections: list[str]
    settings: AnalysisSettings
    status: str = "starting"
    phase: str = "starting"
    level: float = 0.0
    started_at: int | None = None
    processes: list[ProcessHandle] = field(default_factory=list)
    pending: dict[int, dict[int, SecondRecord]] = field(default_factory=dict)
    last_written: int | None = None
    live: deque = field(default_factory=lambda: deque(maxlen=LIVE_POINTS))
    monitor: ClusterMonitor | None = None
    task: asyncio.Task | None = None
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    pause_requested: bool = False
    paused_total: float = 0.0
    paused_since: float | None = None
    load_target_docs: int = 0
    secrets: list[str] = field(default_factory=list)
    errors_live: dict = field(default_factory=dict)
    error: dict | None = None
    stop_reason: str | None = None
    guard: analysis.GuardrailWatch | None = None
    guardrail: dict | None = None
    clean_seconds: int = 0
    last_disruption_alert: float = 0.0
    plan: list = field(default_factory=list)
    plan_index: int = -1
    phase_end: float | None = None
    phase_paused_before: float = 0.0


class RunManager:
    def __init__(self, store: Store, hub: Hub, data_dir, mp_context: str = "spawn", notifier: Notifier | None = None):
        self.store = store
        self.hub = hub
        self.data_dir = data_dir
        self.notifier = notifier
        self.ctx: Any = multiprocessing.get_context(mp_context)
        self.active: ActiveRun | None = None
        self.redactions: list[str] = []
        self._lock = asyncio.Lock()

    # ---- helpers ----

    def redact(self, text: str) -> str:
        if not isinstance(text, str):
            return text
        for secret in self.redactions + (self.active.secrets if self.active else []):
            if secret:
                text = text.replace(secret, MASK)
        return text

    def register_secrets(self, uri: str) -> None:
        for secret in secrets_in(uri):
            if secret not in self.redactions:
                self.redactions.append(secret)
        self.redactions.sort(key=len, reverse=True)

    def status(self) -> dict | None:
        return self._state(self.active) if self.active is not None else None

    def _state(self, run: ActiveRun) -> dict:
        last = run.live[-1] if run.live else None
        return {
            "id": run.id,
            "name": run.name,
            "status": run.status,
            "phase": run.phase,
            "level": run.level,
            "model": run.profile.load.model,
            "target_rate": run.profile.load.rate if run.profile.load.model == "open" else None,
            "started_at": run.started_at,
            "elapsed_ms": self._elapsed_ms(run),
            "last": last,
            "load": {"target": run.load_target_docs * len(run.collections), "done": sum(p.loaded for p in run.processes)}
            if run.status == "loading"
            else None,
            "errors": run.errors_live,
            "progress": self._progress(run),
            "guardrail": run.guardrail,
            "guardrails": run.profile.guardrails.model_dump() if run.guard else None,
        }

    def _progress(self, run: ActiveRun) -> dict | None:
        if run.plan_index < 0 or not run.plan:
            return None
        phase_remaining = None
        if run.phase_end is not None:
            phase_remaining = max(0.0, run.phase_end + self._paused_seconds(run) - run.phase_paused_before - time.monotonic())
        later = [seconds for _, seconds in run.plan[run.plan_index + 1 :]]
        total_remaining = None if phase_remaining is None or any(s is None for s in later) else phase_remaining + sum(later)
        planned = None if any(s is None for _, s in run.plan) else sum(s for _, s in run.plan)
        return {
            "phases": [{"name": n, "seconds": s} for n, s in run.plan],
            "index": run.plan_index,
            "phase_seconds": run.plan[run.plan_index][1],
            "phase_remaining_s": None if phase_remaining is None else round(phase_remaining, 1),
            "total_planned_s": planned,
            "total_remaining_s": None if total_remaining is None else round(total_remaining, 1),
        }

    def _elapsed_ms(self, run: ActiveRun) -> int | None:
        if run.started_at is None:
            return None
        paused = self._paused_seconds(run)
        return int(now_ms() - run.started_at - paused * 1000)

    def _set_status(self, run: ActiveRun, status: str, **fields) -> None:
        run.status = status
        self.store.update_run(run.id, status=status, **fields)
        self.hub.publish("state", self.status() or {"id": run.id, "status": status})

    # ---- preflight and safety ----

    async def preflight(self, target: dict, profile: Profile) -> dict:
        """Check what a run would touch, without writing anything."""
        self.register_secrets(target["uri"])
        client = AsyncMongoClient(
            target["uri"], serverSelectionTimeoutMS=8000, appname=f"{product.SLUG}-preflight", **client_options(target)
        )
        try:
            db = client[profile.dataset.database]
            names = sorted(await db.list_collection_names())
            registry = set()
            if product.REGISTRY_COLLECTION in names:
                async for doc in db[product.REGISTRY_COLLECTION].find({}, {"_id": 1}):
                    registry.add(doc["_id"])
            ours = [n for n in names if n in registry]
            foreign = [n for n in names if n not in registry and n != product.REGISTRY_COLLECTION and not n.startswith("system.")]
            planned = profile.dataset.collection_names()
            clash = [n for n in planned if n in foreign]
            counts = {}
            for name in planned:
                if name in names:
                    counts[name] = await db[name].estimated_document_count()
            return {
                "database": profile.dataset.database,
                "existing_collections": names,
                "ballast_collections": ours,
                "foreign_collections": foreign,
                "name_clash": clash,
                "document_counts": counts,
                "writes": profile.operations.has_writes() or profile.dataset.load.documents > 0 or profile.dataset.load.size_mb > 0,
            }
        except Exception as exc:
            info = explain_failure(exc, self.redact)
            raise RunError(info["title"], info["detail"], info["fix"], status=502) from exc
        finally:
            await client.close()

    def load_target(self, profile: Profile) -> int:
        spec = profile.dataset.load
        docs = spec.documents
        if spec.size_mb > 0:
            size = DocumentFactory(profile.document, __import__("random").Random(0)).approx_size()
            docs = max(docs, math.ceil(spec.size_mb * 1024 * 1024 / max(size, 1)))
        return int(docs)

    async def _prepare_collections(self, client: AsyncMongoClient, profile: Profile, run_id: str) -> dict[str, int]:
        db = client[profile.dataset.database]
        registry = db[product.REGISTRY_COLLECTION]
        existing = set(await db.list_collection_names())
        sizes: dict[str, int] = {}
        for name in profile.dataset.collection_names():
            if name not in existing:
                await db.create_collection(name)
                await registry.update_one(
                    {"_id": name},
                    {"$setOnInsert": {"product": product.NAME, "created_at": datetime.now(UTC), "run_id": run_id}},
                    upsert=True,
                )
            for index in profile.dataset.indexes:
                keys = list(index.keys.items())
                await db[name].create_index(keys, unique=index.unique)
            top = await db[name].find({"_id": {"$type": "number"}}, {"_id": 1}).sort("_id", -1).limit(1).to_list()
            sizes[name] = int(top[0]["_id"]) + 1 if top else 0
        return sizes

    # ---- start ----

    async def start(self, *, name: str, target: dict, profile: Profile, acknowledge_foreign: bool = False) -> dict:
        async with self._lock:
            if self.active is not None:
                raise RunError(
                    "A run is already active",
                    f"Run '{self.active.name}' is {self.active.status}.",
                    "Stop it before starting another.",
                    status=409,
                )
            pre = await self.preflight(target, profile)
            if pre["name_clash"]:
                raise RunError(
                    "Collection name clash",
                    f"{', '.join(pre['name_clash'])} already exist in {pre['database']} and were not created by {product.NAME}.",
                    "Choose another collection prefix or database.",
                    status=409,
                    extra={"preflight": pre},
                )
            if pre["writes"] and pre["foreign_collections"] and not acknowledge_foreign:
                raise RunError(
                    "Confirmation needed",
                    f"{pre['database']} contains collections {product.NAME} did not create: {', '.join(pre['foreign_collections'][:20])}.",
                    "Writes go only to the benchmark collections, but they share the database's cache and disk. Confirm to continue.",
                    status=428,
                    extra={"preflight": pre, "needs": "acknowledge_foreign"},
                )
            run_id = new_id()
            self.register_secrets(target["uri"])
            row = self.store.create_run(run_id, name, target, profile.dataset.database, profile.model_dump(mode="json"))
            settings = AnalysisSettings.from_dict(profile.analysis.model_dump(exclude_none=True))
            run = ActiveRun(
                id=run_id,
                name=name,
                profile=profile,
                target=target,
                database=profile.dataset.database,
                collections=profile.dataset.collection_names(),
                settings=settings,
                secrets=secrets_in(target["uri"]),
            )
            g = profile.guardrails
            if g.enabled():
                run.guard = analysis.GuardrailWatch(g.max_error_percent, g.max_p99_ms, g.sustain_seconds)
            self.active = run
            run.task = asyncio.create_task(self._drive(run, pre))
            self.hub.publish("state", self._state(run))
            self._notify("run_started", row, f"run '{name}' started against {target['name']} ({profile.plain_summary()})")
            return row

    async def _snapshot(self, client: AsyncMongoClient, run: ActiveRun, pre: dict) -> dict:
        hello = await client.admin.command("hello")
        try:
            version = (await client.admin.command("buildInfo")).get("version")
        except Exception:
            version = None
        description = client.topology_description
        members = [{"address": f"{a[0]}:{a[1]}", "state": sd.server_type_name} for a, sd in description.server_descriptions().items()]
        try:
            mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError, AttributeError):
            mem = None
        from ballast import buildinfo

        return {
            "app": {"name": product.NAME, "version": product.VERSION, "commit": buildinfo.COMMIT, "built": buildinfo.BUILT},
            "driver": {"name": "pymongo", "version": pymongo.version},
            "python": platform.python_version(),
            "server": {
                "version": version,
                "topology": description.topology_type_name if hello.get("msg") != "isdbgrid" else "Sharded",
                "set_name": hello.get("setName"),
                "primary": hello.get("primary"),
                "members": sorted(members, key=lambda m: m["address"]),
            },
            "target": {"name": run.target["name"], "uri": redact_uri(run.target["uri"]), "tls": run.target.get("tls", "auto")},
            "effective_driver_options": run.profile.driver.client_options(),
            "generator_host": {
                "hostname": platform.node(),
                "cpu_count": os.cpu_count(),
                "memory_bytes": mem,
                "platform": platform.platform(),
            },
            "preflight": {k: pre[k] for k in ("database", "foreign_collections", "document_counts")},
            "profile_summary": run.profile.plain_summary(),
        }

    async def _drive(self, run: ActiveRun, pre: dict) -> None:
        client = None
        reader_fds: list[int] = []
        loop = asyncio.get_running_loop()
        try:
            client = AsyncMongoClient(
                run.target["uri"], serverSelectionTimeoutMS=15000, appname=f"{product.SLUG}-control", **client_options(run.target)
            )
            sizes = await self._prepare_collections(client, run.profile, run.id)
            snapshot = await self._snapshot(client, run, pre)
            self.store.update_run(run.id, snapshot=snapshot)
            run.monitor = ClusterMonitor(
                run.target["uri"],
                client_options(run.target),
                self._on_monitor_event,
                self._on_sample,
                run.profile.sampling.enabled,
                run.profile.sampling.interval_seconds,
                self.redact,
            )
            try:
                await run.monitor.start()
            except Exception as exc:
                self._record_event(run, now_ms(), "monitor_unavailable", {"error": self.redact(str(exc))[:200]})
            spec = {
                "run_id": run.id,
                "uri": run.target["uri"],
                "client_options": client_options(run.target),
                "profile": run.profile.model_dump(mode="json"),
                "database": run.database,
                "collections": run.collections,
                "key_sizes": sizes,
                "app_name": f"{product.SLUG}-gen",
                "secrets": run.secrets,
            }
            count = run.profile.load.processes
            for index in range(count):
                parent_conn, child_conn = self.ctx.Pipe()
                from ballast.engine import generator

                proc = self.ctx.Process(
                    target=generator.entry,
                    args=(child_conn, {**spec, "process_index": index, "process_count": count}),
                    name=f"ballast-gen-{index}",
                    daemon=True,
                )
                proc.start()
                child_conn.close()
                handle = ProcessHandle(index=index, process=proc, conn=parent_conn)
                run.processes.append(handle)
                loop.add_reader(parent_conn.fileno(), self._on_child_readable, run, handle)
                reader_fds.append(parent_conn.fileno())
            await self._wait(run, lambda: all(p.ready for p in run.processes), 60, "Generator processes did not connect")
            run.load_target_docs = self.load_target(run.profile)
            if run.load_target_docs > 0 and any(size < run.load_target_docs for size in sizes.values()):
                self._set_status(run, "loading")
                run.phase = "load"
                self._record_event(run, now_ms(), "phase", {"phase": "load", "documents_per_collection": run.load_target_docs})
                for p in run.processes:
                    p.conn.send(
                        {
                            "cmd": "load",
                            "targets": {n: run.load_target_docs for n in run.collections},
                            "batch_size": run.profile.dataset.load.batch_size,
                        }
                    )
                await self._wait(run, lambda: all(p.load_done for p in run.processes), None, "Data load did not finish")
                self._record_event(run, now_ms(), "load_done", {"documents": sum(p.loaded for p in run.processes)})
            if run.stop_requested.is_set():
                return
            run.started_at = now_ms()
            self._set_status(run, "running", started_at=run.started_at)
            await self._schedule(run)
        except asyncio.CancelledError:
            raise
        except RunError as exc:
            run.error = exc.as_dict()
        except Exception as exc:
            log.exception("run %s failed", run.id)
            info = (
                explain_failure(exc, self.redact)
                if not isinstance(exc, RuntimeError)
                else {"title": "Run failed", "detail": self.redact(str(exc)), "fix": ""}
            )
            run.error = info
        finally:
            await self._finish(run, client, reader_fds)

    async def _wait(self, run: ActiveRun, predicate, timeout: float | None, message: str) -> None:
        deadline = time.monotonic() + timeout if timeout else None
        while not predicate():
            if run.error:
                raise RunError(run.error.get("title", message), run.error.get("detail", ""), run.error.get("fix", ""))
            if run.stop_requested.is_set():
                return
            if any(not p.process.is_alive() and not p.stopped for p in run.processes):
                raise RunError("A generator process exited unexpectedly", message, "See logs/ballast.log for details.")
            if deadline and time.monotonic() > deadline:
                raise RunError(message, f"Waited {timeout:.0f} s.", "Check that this server can reach the cluster.")
            await asyncio.sleep(0.1)

    def _send_all(self, message: dict) -> None:
        for p in self.active.processes if self.active else []:
            try:
                p.conn.send(message)
            except (BrokenPipeError, OSError):
                pass

    async def _phase(self, run: ActiveRun, phase: str, level: float, seconds: float | None, first: bool) -> bool:
        """Run one phase. Returns False if the run was stopped."""
        run.phase, run.level = phase, level
        self._send_all({"cmd": "run" if first else "set", "phase": phase, "level": level})
        self._record_event(run, now_ms(), "phase", {"phase": phase, "level": level, "seconds": seconds})
        end = None if seconds is None else time.monotonic() + seconds
        paused_before = self._paused_seconds(run)
        run.plan_index = next((i for i, (name, _) in enumerate(run.plan) if name == phase), run.plan_index)
        run.phase_end, run.phase_paused_before = end, paused_before
        self.hub.publish("state", self._state(run))
        while True:
            if run.stop_requested.is_set():
                return False
            if end is not None and run.paused_since is None and time.monotonic() >= end + self._paused_seconds(run) - paused_before:
                return True
            await asyncio.sleep(0.2)

    @staticmethod
    def _paused_seconds(run: ActiveRun) -> float:
        return run.paused_total + ((time.monotonic() - run.paused_since) if run.paused_since else 0.0)

    async def _schedule(self, run: ActiveRun) -> None:
        sched = run.profile.schedule
        target = run.profile.target_level()
        first = True
        plan: list[tuple[str, int | None]] = [("warmup", sched.warmup_seconds)] if sched.warmup_seconds > 0 else []
        run.plan = plan + [(f"ramp {i + 1}", step.seconds) for i, step in enumerate(sched.ramp)]
        run.plan.append(("steady", sched.steady_seconds))
        if sched.warmup_seconds > 0:
            if not await self._phase(
                run, "warmup", run.profile.schedule.ramp[0].level if sched.ramp else target, sched.warmup_seconds, first
            ):
                return
            first = False
        for i, step in enumerate(sched.ramp):
            if not await self._phase(run, f"ramp {i + 1}", step.level, step.seconds, first):
                return
            first = False
        if await self._phase(run, "steady", target, sched.steady_seconds, first):
            run.stop_reason = "completed"

    # ---- child messages ----

    def _on_child_readable(self, run: ActiveRun, handle: ProcessHandle) -> None:
        try:
            while handle.conn.poll():
                self._handle_child(run, handle, handle.conn.recv())
        except (EOFError, OSError):
            handle.stopped = True
            try:
                asyncio.get_running_loop().remove_reader(handle.conn.fileno())
            except Exception:
                pass

    def _handle_child(self, run: ActiveRun, handle: ProcessHandle, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "second":
            record = SecondRecord.from_json(msg["data"])
            run.pending.setdefault(record.t, {})[handle.index] = record
            self._flush_pending(run)
        elif kind == "gap":
            self.store.add_gap(run.id, msg["process"], msg["kind"], msg["start_ms"], msg["end_ms"])
            if run.profile.load.processes == 1:
                self.hub.publish("gap", {"run": run.id, "kind": msg["kind"], "start_ms": msg["start_ms"], "end_ms": msg["end_ms"]})
        elif kind == "event":
            self._record_event(run, msg["t_ms"], msg["kind"], msg["data"])
        elif kind == "ready":
            handle.ready = True
        elif kind == "load_progress":
            handle.loaded = msg["docs"]
            self.hub.publish(
                "load", {"run": run.id, "done": sum(p.loaded for p in run.processes), "target": run.load_target_docs * len(run.collections)}
            )
        elif kind == "load_done":
            handle.loaded = msg["docs"]
            handle.load_done = True
        elif kind == "drained":
            handle.drained = True
            if msg.get("forced"):
                self._record_event(run, now_ms(), "drain_forced", {"process": handle.index, "in_flight": msg["forced"]})
        elif kind == "stopped":
            handle.stopped = True
        elif kind == "error":
            err = msg.get("error", {})
            run.error = {
                "title": f"Generator failed during {msg.get('stage')}",
                "detail": self.redact(err.get("message", "")),
                "fix": "",
                "class": err.get("class"),
            }
            if msg.get("stage") == "connect":
                run.error["fix"] = "Test the target's connection from the Targets page."
            run.stop_requested.set()

    def _flush_pending(self, run: ActiveRun, force: bool = False) -> None:
        if not run.pending:
            return
        expected = len(run.processes)
        newest = max(run.pending)
        for t in sorted(run.pending):
            parts = run.pending[t]
            if len(parts) < expected and not force and newest - t < STRAGGLER_SECONDS:
                break
            records = list(parts.values())
            merged = records[0]
            for other in records[1:]:
                merged.merge(other)
            del run.pending[t]
            if run.last_written is not None and t <= run.last_written:
                for cls, n in merged.errors.items():
                    run.errors_live[cls] = run.errors_live.get(cls, 0) + n
                stored = self.store.seconds(run.id, t, t + 1)
                if stored:
                    stored[0].merge(merged)
                    merged = stored[0]
                self.store.add_second(run.id, merged)
                continue
            run.last_written = t
            self.store.add_second(run.id, merged)
            point = merged.point()
            run.live.append(point)
            for cls, n in merged.errors.items():
                run.errors_live[cls] = run.errors_live.get(cls, 0) + n
            self.hub.publish("point", {"run": run.id, "point": point})
            self._watch_second(run, merged)

    def _notify(self, event: str, run: dict, text: str) -> None:
        if self.notifier is not None:
            try:
                self.notifier.notify(event, run, text)
            except Exception:
                log.exception("notification %s failed", event)

    def _watch_second(self, run: ActiveRun, record: SecondRecord) -> None:
        if run.status not in ("running",):
            return
        if run.guard is not None:
            trip = run.guard.observe(record)
            if trip is not None:
                run.guardrail = trip
                self._record_event(run, now_ms(), "guardrail", trip)
                self._notify(
                    "guardrail",
                    {"id": run.id, "name": run.name, "target_name": run.target["name"]},
                    f"run '{run.name}' stopped by a guardrail: {'; '.join(trip['reasons'])}",
                )
                asyncio.get_running_loop().create_task(self.stop(run.id, reason="guardrail"))
                return
        if record.phase in analysis.EXCLUDED_PHASES:
            return
        if record.err == 0:
            run.clean_seconds += 1
            return
        if run.clean_seconds >= 30 and time.monotonic() - run.last_disruption_alert > 60:
            run.last_disruption_alert = time.monotonic()
            classes = ", ".join(f"{n} {c}" for c, n in sorted(record.errors.items(), key=lambda kv: -kv[1])[:3])
            self._notify(
                "disruption",
                {"id": run.id, "name": run.name, "target_name": run.target["name"]},
                f"run '{run.name}': operations started failing ({classes})",
            )
        run.clean_seconds = 0

    def _record_event(self, run: ActiveRun, t_ms: int, kind: str, data: dict) -> dict:
        event = self.store.add_event(run.id, t_ms, kind, data)
        self.hub.publish("event", {"run": run.id, **event})
        return event

    def _on_monitor_event(self, t_ms: int, kind: str, data: dict) -> None:
        if self.active is not None:
            self._record_event(self.active, t_ms, kind, data)

    def _on_sample(self, t_ms: int, kind: str, data: dict) -> None:
        if self.active is not None:
            self.store.add_sample(self.active.id, t_ms, kind, data)
            self.hub.publish("sample", {"run": self.active.id, "t_ms": t_ms, "kind": kind, "data": data})

    # ---- controls ----

    def _require(self, run_id: str) -> ActiveRun:
        run = self.active
        if run is None or (run_id not in (run.id, "active")):
            raise RunError("Run is not active", "Only an active run can be controlled.", "", status=409)
        return run

    def pause(self, run_id: str) -> dict:
        run = self._require(run_id)
        if run.status != "running":
            raise RunError("Run is not running", f"The run is {run.status}.", "", status=409)
        run.paused_since = time.monotonic()
        self._send_all({"cmd": "pause"})
        self._record_event(run, now_ms(), "paused", {})
        self._set_status(run, "paused")
        return self._state(run)

    def resume(self, run_id: str) -> dict:
        run = self._require(run_id)
        if run.status != "paused":
            raise RunError("Run is not paused", f"The run is {run.status}.", "", status=409)
        run.paused_total += time.monotonic() - (run.paused_since or time.monotonic())
        run.paused_since = None
        self._send_all({"cmd": "resume"})
        self._record_event(run, now_ms(), "resumed", {})
        self._set_status(run, "running")
        return self._state(run)

    def set_level(self, run_id: str, level: float) -> dict:
        run = self._require(run_id)
        if run.status not in ("running", "paused"):
            raise RunError("Run is not running", f"The run is {run.status}.", "", status=409)
        run.level = level
        self._send_all({"cmd": "set", "phase": run.phase, "level": level})
        self._record_event(run, now_ms(), "level_changed", {"level": level, "model": run.profile.load.model})
        self.hub.publish("state", self._state(run))
        return self._state(run)

    async def stop(self, run_id: str, reason: str = "stopped") -> dict:
        run = self._require(run_id)
        if not run.stop_requested.is_set():
            run.stop_reason = reason
            run.stop_requested.set()
            self._record_event(run, now_ms(), "stop_requested", {"reason": reason})
            if run.status in ("running", "paused"):
                self._set_status(run, "stopping")
        return {"id": run.id, "status": run.status}

    def add_marker(
        self, run_id: str, label: str, note: str = "", phase: str | None = None, source: str = "api", t_ms: int | None = None
    ) -> dict:
        run = self.active if run_id == "active" else None
        target_id = run.id if run else run_id
        row = self.store.get_run(target_id)
        if row is None:
            raise RunError("Run not found", "", "", status=404)
        if row["status"] not in ACTIVE:
            raise RunError("Run is not active", "Markers can only be added while a run is live.", "Start a run first.", status=409)
        marker = self.store.add_marker(target_id, t_ms or now_ms(), label, note, phase, source)
        self.hub.publish("marker", {"run": target_id, **marker})
        return marker

    # ---- finish ----

    async def _finish(self, run: ActiveRun, client, reader_fds: list[int]) -> None:
        loop = asyncio.get_running_loop()
        drain = run.profile.schedule.drain_timeout_seconds
        if run.status not in ("stopping",):
            run.status = "stopping"
            self.hub.publish("state", self._state(run))
        self._send_all({"cmd": "stop", "drain": drain})
        deadline = time.monotonic() + drain + 10
        while time.monotonic() < deadline and not all(p.stopped or not p.process.is_alive() for p in run.processes):
            await asyncio.sleep(0.1)
        for p in run.processes:
            if p.process.is_alive():
                p.process.terminate()
                await asyncio.sleep(0.5)
                if p.process.is_alive():
                    p.process.kill()
                self._record_event(run, now_ms(), "generator_killed", {"process": p.index})
        for fd in reader_fds:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
        for p in run.processes:
            try:
                p.conn.close()
            except Exception:
                pass
            p.process.join(timeout=2)
        self._flush_pending(run, force=True)
        if run.monitor is not None:
            try:
                await run.monitor.stop()
            except Exception:
                pass
        if client is not None:
            await client.close()
        if run.error:
            status = "failed"
        elif run.stop_reason == "completed":
            status = "completed"
        else:
            status = "stopped"
        ended = now_ms()
        analysis_result = None
        summary = None
        try:
            analysis_result = self.analyze(run.id)
            summary = analysis_result["summary"]
        except Exception:
            log.exception("analysis failed for run %s", run.id)
        slo = (analysis_result or {}).get("slo")
        self.store.update_run(
            run.id,
            status=status,
            ended_at=ended,
            summary=summary,
            analysis=analysis_result,
            error=run.error,
            stop_reason=run.stop_reason,
            slo_passed=slo.get("passed") if slo else None,
        )
        self.active = None
        self.hub.publish("state", {"id": run.id, "status": status, "ended_at": ended, "error": run.error})
        log.info("run %s finished with status %s", run.id, status)
        text = f"run '{run.name}' {status}"
        if run.stop_reason == "guardrail" and run.guardrail:
            text += " by a guardrail (" + "; ".join(run.guardrail["reasons"]) + ")"
        if summary:
            rt = summary.get("response_time", {})
            text += (
                f": {summary.get('throughput', 0):,.0f} ops/s, p99 {rt.get('p99_us', 0) / 1000:,.1f} ms, {summary.get('err', 0):,} failed"
            )
        if slo and slo.get("passed") is not None:
            text += ", SLO " + ("passed" if slo["passed"] else "failed")
        impacts = len((analysis_result or {}).get("impacts", []))
        if impacts:
            text += f", {impacts} impact window(s)"
        self._notify("run_finished", self.store.get_run(run.id) or {"id": run.id, "name": run.name}, text)

    async def shutdown(self) -> None:
        if self.active is not None:
            await self.stop(self.active.id, reason="server_shutdown")
            if self.active and self.active.task:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self.active.task), timeout=self.active.profile.schedule.drain_timeout_seconds + 15
                    )
                except (TimeoutError, asyncio.CancelledError):
                    pass

    # ---- reading ----

    def analyze(self, run_id: str, extra_window: dict | None = None) -> dict:
        row = self.store.get_run(run_id)
        if row is None:
            raise RunError("Run not found", status=404)
        profile = row["profile"] or {}
        settings = AnalysisSettings.from_dict({k: v for k, v in (profile.get("analysis") or {}).items() if v is not None})
        records = self.store.seconds(run_id)
        processes = (profile.get("load") or {}).get("processes", 1)
        gaps = intersect_gaps(self.store.gaps(run_id), processes)
        events = self.store.events(run_id)
        markers = self.store.markers(run_id)
        if extra_window is not None:
            return analysis.analyze_window(extra_window, records, gaps, events, settings, analysis.marker_windows(markers))
        result = analysis.full_analysis(records, gaps, events, markers, settings)
        load = profile.get("load") or {}
        target_rate = load.get("rate") if load.get("model", "open") == "open" else None
        criteria = {k: v for k, v in (profile.get("slo") or {}).items() if v is not None}
        result["slo"] = analysis.evaluate_slo(result["summary"], criteria, target_rate)
        return result

    def series(self, run_id: str, max_points: int = 1500, start: int | None = None, end: int | None = None) -> dict:
        records = self.store.seconds(run_id, start, end)
        step = 1
        if len(records) > max_points:
            span = records[-1].t - records[0].t + 1
            step = max(1, math.ceil(span / max_points))
            for nice in (2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
                if nice >= step:
                    step = nice
                    break
            records = downsample(records, step)
        return {"step": step, "points": [r.point() for r in records]}
