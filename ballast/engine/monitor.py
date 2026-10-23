"""Cluster monitor for a run: topology events from the driver plus optional server sampling.

Runs in the server process with its own small client and a fast heartbeat so
elections and member state changes are timestamped within about half a second.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from pymongo import AsyncMongoClient, monitoring
from pymongo import errors as pe

log = logging.getLogger("ballast.monitor")

UNAUTHORIZED_CODES = {13, 18, 8000}


def _addr(address) -> str:
    return f"{address[0]}:{address[1]}" if address else ""


def primary_of(description) -> str | None:
    for address, sd in description.server_descriptions().items():
        if sd.server_type_name == "RSPrimary":
            return _addr(address)
    return None


class _TopologyEvents(monitoring.TopologyListener, monitoring.ServerListener, monitoring.ServerHeartbeatListener):
    def __init__(self, emit: Callable[[str, dict], None]):
        self.emit = emit
        self.failing: set[str] = set()
        self.last_primary: str | None = None

    def opened(self, event):
        pass

    def closed(self, event):
        pass

    def started(self, event):
        pass

    def description_changed(self, event):
        previous, new = event.previous_description, event.new_description
        if isinstance(event, monitoring.TopologyDescriptionChangedEvent):
            before, after = primary_of(previous), primary_of(new)
            if before != after:
                if after is None:
                    self.emit("primary_lost", {"from": before})
                elif after != self.last_primary:
                    self.emit("primary_changed", {"from": self.last_primary, "to": after})
                else:
                    self.emit("primary_restored", {"to": after})
                if after is not None:
                    self.last_primary = after
            if previous.topology_type_name != new.topology_type_name and previous.topology_type_name != "Unknown":
                self.emit("topology_changed", {"from": previous.topology_type_name, "to": new.topology_type_name})
        else:
            if previous.server_type_name != new.server_type_name:
                data = {"address": _addr(event.server_address), "from": previous.server_type_name, "to": new.server_type_name}
                if new.error is not None:
                    data["error"] = str(new.error).split("\n")[0][:200]
                self.emit("server_state", data)

    def succeeded(self, event):
        address = _addr(event.connection_id)
        if address in self.failing:
            self.failing.discard(address)
            self.emit("heartbeat_recovered", {"address": address})

    def failed(self, event):
        address = _addr(event.connection_id)
        if address not in self.failing:
            self.failing.add(address)
            self.emit("heartbeat_failed", {"address": address, "error": str(event.reply).split("\n")[0][:200]})


class ClusterMonitor:
    def __init__(
        self,
        uri: str,
        client_options: dict,
        on_event: Callable[[int, str, dict], None],
        on_sample: Callable[[int, str, dict], None],
        sampling: bool,
        interval: float,
        redact=None,
    ):
        self.uri = uri
        self.client_options = client_options
        self.on_event = on_event
        self.on_sample = on_sample
        self.sampling = sampling
        self.interval = interval
        self.redact = redact or (lambda s: s)
        self.client: AsyncMongoClient | None = None
        self.task: asyncio.Task | None = None
        self.unavailable: set[str] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.closing = False

    def _emit(self, kind: str, data: dict) -> None:
        if self.closing:
            return
        t_ms = int(time.time() * 1000)
        for key in ("error",):
            if key in data and isinstance(data[key], str):
                data[key] = self.redact(data[key])
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.on_event, t_ms, kind, data)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        options = dict(self.client_options)
        options.update(heartbeatFrequencyMS=500, serverSelectionTimeoutMS=2000, maxPoolSize=2, minPoolSize=0, appname="ballast-monitor")
        self.client = AsyncMongoClient(self.uri, event_listeners=[_TopologyEvents(self._emit)], **options)
        await self.client.aconnect()
        if self.sampling:
            self.task = asyncio.create_task(self._sample_loop())

    async def stop(self) -> None:
        self.closing = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.client is not None:
            await self.client.close()

    async def _command(self, name: str, command: dict) -> dict | None:
        if name in self.unavailable:
            return None
        assert self.client is not None
        try:
            return await self.client.admin.command(command)
        except pe.OperationFailure as exc:
            if exc.code in UNAUTHORIZED_CODES or "not authorized" in str(exc).lower() or "unauthorized" in str(exc).lower():
                self.unavailable.add(name)
                self.on_event(
                    int(time.time() * 1000),
                    "sampling_unavailable",
                    {
                        "command": name,
                        "reason": self.redact(str(exc).split("\n")[0][:200]),
                        "fix": "Grant the clusterMonitor role to this user to enable server sampling. The run continues without it.",
                    },
                )
                return None
            if name == "replSetGetStatus" and exc.code in (76, 94):
                self.unavailable.add(name)
                return None
            raise

    async def _sample_loop(self) -> None:
        while True:
            t_ms = int(time.time() * 1000)
            try:
                hello = await self._command("hello", {"hello": 1})
                if hello:
                    self.on_sample(
                        t_ms,
                        "hello",
                        {
                            k: hello.get(k)
                            for k in ("isWritablePrimary", "secondary", "setName", "primary", "me", "hosts", "msg")
                            if k in hello
                        },
                    )
                status = await self._command("serverStatus", {"serverStatus": 1, "repl": 1, "metrics": 0, "locks": 0, "wiredTiger": 0})
                if status:
                    self.on_sample(
                        t_ms,
                        "serverStatus",
                        {
                            "host": status.get("host"),
                            "version": status.get("version"),
                            "uptime": status.get("uptime"),
                            "connections": status.get("connections"),
                            "opcounters": status.get("opcounters"),
                            "network": {k: status.get("network", {}).get(k) for k in ("bytesIn", "bytesOut", "numRequests")},
                        },
                    )
                rs = await self._command("replSetGetStatus", {"replSetGetStatus": 1})
                if rs:
                    members = []
                    primary_optime = None
                    for m in rs.get("members", []):
                        if m.get("stateStr") == "PRIMARY":
                            primary_optime = m.get("optimeDate")
                    for m in rs.get("members", []):
                        lag = None
                        if primary_optime and m.get("optimeDate"):
                            lag = round((primary_optime - m["optimeDate"]).total_seconds(), 3)
                        members.append({"name": m.get("name"), "state": m.get("stateStr"), "health": m.get("health"), "lag_seconds": lag})
                    self.on_sample(t_ms, "replSetGetStatus", {"set": rs.get("set"), "members": members})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.on_sample(t_ms, "error", {"error": self.redact(str(exc).split("\n")[0][:200])})
            await asyncio.sleep(self.interval)
